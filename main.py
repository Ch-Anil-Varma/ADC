import discord
from discord.ext import commands, tasks
from discord import ui
import os
import google.generativeai as genai
import asyncio
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient
from keep_alive import keep_alive

# --- Setup ---
load_dotenv()
TOKEN = os.getenv("TOKEN")
MONGO_URL = os.getenv("MONGO_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") 

# --- Database ---
cluster = MongoClient(MONGO_URL)
db = cluster["CollegeBot"]
questions_col = db["questions"]
submissions_col = db["submissions"]
users_col = db["users"]

# --- Global Cache for Timers ---
attempt_timers = {}

# --- AI Setup ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash', generation_config={"response_mime_type": "application/json"})

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# --- CONFIGURATION ---
LECTURER_ROLE_NAME = "Lecturer"
STUDENT_ROLE_NAME = "Student"
QUESTIONS_CHANNEL_ID = 1466759973324329007
LEADERBOARD_CHANNEL_ID = 1466759973324329008

# --- UI COLORS ---
COLOR_PRIMARY = 0x5865F2  # Discord Blurple
COLOR_SUCCESS = 0x57F287  # Green
COLOR_WARNING = 0xFEE75C  # Yellow
COLOR_DANGER = 0xED4245   # Red
COLOR_GOLD = 0xFFD700     # Gold

# --- Helper: Grade with AI ---
async def grade_submission(title, desc, code, lang):
    prompt = f"""
    Role: Senior Computer Science Professor.
    Task 1: Grade the code strictly based on correctness and efficiency.
    Task 2: Detect AI generation (ChatGPT style).

    Question: {title}
    Description: {desc}
    Language: {lang}
    Code:
    {code}

    OUTPUT JSON:
    {{
        "score": (0-100),
        "feedback": "(Professional, constructive feedback. Max 2 sentences.)",
        "status": "Pass" or "Fail",
        "is_ai_suspected": (true/false)
    }}
    """
    try:
        response = await model.generate_content_async(prompt)
        if not response.parts:
            return {"score": 0, "feedback": "Code flagged by AI Safety filters.", "status": "Fail", "is_ai_suspected": False}
        
        raw_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(raw_text)

    except Exception as e:
        print(f"❌ AI ERROR: {e}")
        return {"score": 0, "feedback": "System Error. Please notify the Lecturer.", "status": "Fail", "is_ai_suspected": False}

# --- Helper: Update Live Leaderboard ---
async def update_live_leaderboard(question_id, guild):
    question_data = questions_col.find_one({"_id": question_id})
    if not question_data or "leaderboard_msg_id" not in question_data:
        return

    # Get Top 50 Submissions
    subs = list(submissions_col.find({"question_id": question_id}).sort([("score", -1), ("duration_seconds", 1)]))

    # Build Description
    desc = f"**Problem:** {question_data['title']}\n**Total Submissions:** {len(subs)}\n"
    desc += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    
    if not subs:
        desc += "*Waiting for the first brave student...* 🕒"
    else:
        # Show Top 50
        for i, sub in enumerate(subs[:50], 1):
            user = guild.get_member(sub["user_id"])
            username = user.display_name if user else "Unknown Student"
            
            # Time formatting
            minutes = int(sub['duration_seconds'] // 60)
            seconds = int(sub['duration_seconds'] % 60)
            time_str = f"{minutes}m {seconds}s"
            
            # Medals
            if i == 1: icon = "🥇"
            elif i == 2: icon = "🥈"
            elif i == 3: icon = "🥉"
            elif i <= 10: icon = "🏅"
            else: icon = f"**{i}.**"

            desc += f"{icon} `{username}` • **{sub['score']}** pts • *{time_str}*\n"

    embed = discord.Embed(
        title=f"📊 Live Leaderboard: {question_data['title']}", 
        description=desc, 
        color=COLOR_GOLD,
        timestamp=datetime.utcnow()
    )
    if bot.user.avatar:
        embed.set_thumbnail(url=bot.user.avatar.url)
    embed.set_footer(text="Updates in real-time • Ranked by Score & Speed")

    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if channel:
        try:
            msg = await channel.fetch_message(question_data["leaderboard_msg_id"])
            await msg.edit(embed=embed)
        except:
            pass

# --- UI: Code Submission Modal ---
class CodeModal(ui.Modal, title="Submit Your Code"):
    # INCREASED LENGTH TO 4000 (Discord Max)
    code_input = ui.TextInput(
        label="Paste Your Code Here", 
        style=discord.TextStyle.paragraph, 
        placeholder="void main() { ... }",
        max_length=4000
    )

    def __init__(self, language, question_id, title, desc):
        super().__init__()
        self.language = language
        self.question_id = question_id
        self.title = title
        self.desc = desc

    async def on_submit(self, interaction: discord.Interaction):
        # 1. Timer Logic
        timer_key = f"{interaction.user.id}_{self.question_id}"
        start_time = attempt_timers.get(timer_key)
        duration = 0
        if start_time:
            duration = (datetime.utcnow() - start_time).total_seconds()
            if timer_key in attempt_timers: del attempt_timers[timer_key]

        # 2. Speed Trap (15s)
        if duration < 15:
            embed = discord.Embed(title="⛔ Submission Rejected", description="You answered too fast (<15s). Copy-pasting is not allowed.", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 3. Sloppy Paste Filter
        banned = ["here is the code", "as an ai", "hope this helps"]
        if any(p in self.code_input.value.lower() for p in banned):
            embed = discord.Embed(title="⛔ Submission Rejected", description="AI conversational text detected. Submit ONLY the code.", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 4. Duplicate Check
        if submissions_col.find_one({"user_id": interaction.user.id, "question_id": self.question_id}):
            await interaction.response.send_message("⚠️ You have already submitted.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        # 5. AI Grading
        result = await grade_submission(self.title, self.desc, self.code_input.value, self.language)
        score = result.get("score", 0)
        feedback = result.get("feedback", "No feedback.")
        is_ai_suspected = result.get("is_ai_suspected", False)

        if is_ai_suspected:
            score = 0
            feedback = "⚠️ **AI Detection Alert:** Code style strongly resembles AI generation."

        # 6. Save Data
        submissions_col.insert_one({
            "user_id": interaction.user.id,
            "question_id": self.question_id,
            "score": score,
            "feedback": feedback,
            "language": self.language,
            "duration_seconds": duration,
            "is_ai_flagged": is_ai_suspected,
            "timestamp": datetime.utcnow()
        })

        if score > 0:
            users_col.update_one({"_id": interaction.user.id}, {"$inc": {"score": score}}, upsert=True)

        # 7. Result Embed
        color = COLOR_SUCCESS if score >= 50 else COLOR_DANGER
        if is_ai_suspected: color = COLOR_WARNING
        
        mins, secs = int(duration // 60), int(duration % 60)
        
        embed = discord.Embed(title=f"📝 Grading Result", color=color, timestamp=datetime.utcnow())
        embed.add_field(name="Score", value=f"**{score}/100**", inline=True)
        embed.add_field(name="Time", value=f"`{mins}m {secs}s`", inline=True)
        embed.add_field(name="Language", value=f"`{self.language}`", inline=True)
        embed.add_field(name="Feedback", value=f"*{feedback}*", inline=False)
        embed.set_footer(text="Keep coding!")
        
        await interaction.followup.send(embed=embed, ephemeral=True)

        if not is_ai_suspected:
            await update_live_leaderboard(self.question_id, interaction.guild)

# --- UI: Language Select ---
class LanguageSelect(ui.Select):
    def __init__(self, q_id, title, desc):
        # UPDATED LANGUAGES
        options = [
            discord.SelectOption(label="C", value="C", emoji="🔹", description="Standard C"),
            discord.SelectOption(label="C++", value="C++", emoji="⚙️", description="Standard C++"),
            discord.SelectOption(label="Java", value="Java", emoji="☕", description="Standard Java"),
            discord.SelectOption(label="Python", value="Python", emoji="🐍", description="Python 3"),
        ]
        super().__init__(placeholder="Select Language to Start Timer...", options=options)
        self.q_id = q_id
        self.title = title
        self.desc = desc

    async def callback(self, interaction: discord.Interaction):
        attempt_timers[f"{interaction.user.id}_{self.q_id}"] = datetime.utcnow()
        
        q_data = questions_col.find_one({"_id": self.q_id})
        if not q_data or not q_data.get("active", False):
            await interaction.response.send_message("❌ This question is closed.", ephemeral=True)
            return
            
        await interaction.response.send_modal(CodeModal(self.values[0], self.q_id, self.title, self.desc))

class QuestionView(ui.View):
    def __init__(self, q_id, title, desc):
        super().__init__(timeout=None)
        self.add_item(LanguageSelect(q_id, title, desc))

# --- Bot Events ---
@bot.event
async def on_ready():
    keep_alive()
    print(f"✅ {bot.user} is Online & Professional!")

@bot.command()
@commands.has_role(LECTURER_ROLE_NAME)
async def post(ctx, *, args):
    try:
        parts = args.split('|')
        title = parts[0].strip()
        description = parts[1].strip()
    except:
        await ctx.send(embed=discord.Embed(title="❌ Syntax Error", description="Usage: `!post Title | Description`", color=COLOR_DANGER))
        return

    questions_col.update_many({"active": True}, {"$set": {"active": False}})
    question_id = str(ctx.message.id)
    
    # Initial Leaderboard
    lb_channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    leaderboard_msg = None
    if lb_channel:
        embed = discord.Embed(
            title=f"📊 Live Leaderboard: {title}", 
            description="*Waiting for submissions...* 🕒", 
            color=COLOR_GOLD,
            timestamp=datetime.utcnow()
        )
        if bot.user.avatar: embed.set_thumbnail(url=bot.user.avatar.url)
        leaderboard_msg = await lb_channel.send(embed=embed)

    questions_col.insert_one({
        "_id": question_id,
        "title": title,
        "description": description,
        "active": True,
        "leaderboard_msg_id": leaderboard_msg.id if leaderboard_msg else None,
        "timestamp": datetime.utcnow()
    })

    # Question Post
    q_channel = bot.get_channel(QUESTIONS_CHANNEL_ID)
    role = discord.utils.get(ctx.guild.roles, name=STUDENT_ROLE_NAME)
    
    embed = discord.Embed(title=f"📢 New Challenge: {title}", description=description, color=COLOR_PRIMARY, timestamp=datetime.utcnow())
    embed.add_field(name="⏳ Time Limit", value="24 Hours", inline=True)
    embed.add_field(name="🤖 AI Grading", value="Enabled", inline=True)
    embed.add_field(name="⚠️ Rules", value="• No Copy-Paste\n• No AI Generated Code", inline=False)
    if bot.user.avatar: embed.set_thumbnail(url=bot.user.avatar.url)
    embed.set_footer(text="Select a language below to begin. The timer starts immediately!")

    if q_channel:
        await q_channel.send(content=f"{role.mention}", embed=embed, view=QuestionView(question_id, title, description))
        
    await ctx.message.delete()

@bot.command()
async def global_leaderboard(ctx):
    # TOP 50 Global
    top_users = users_col.find().sort("score", -1).limit(50)
    
    desc = ""
    count = 0
    for i, user_data in enumerate(top_users, 1):
        user = ctx.guild.get_member(user_data["_id"])
        if user:
            username = user.display_name
            if i == 1: icon = "🏆"
            elif i == 2: icon = "🥈"
            elif i == 3: icon = "🥉"
            else: icon = f"**{i}.**"
            
            desc += f"{icon} `{username}` — **{user_data['score']}** pts\n"
            count += 1
    
    if count == 0: desc = "No data yet."

    embed = discord.Embed(title="🏆 Hall of Fame (Top 50)", description=desc, color=discord.Color.purple(), timestamp=datetime.utcnow())
    if bot.user.avatar: embed.set_thumbnail(url=bot.user.avatar.url)
    
    await ctx.send(embed=embed)

bot.run(TOKEN)
