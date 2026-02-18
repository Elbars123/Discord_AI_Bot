import os
import json
import discord
from discord.ext import commands
from anthropic import Anthropic
from datetime import datetime, date
import asyncio
from notion_client import Client as NotionClient

# ─── 설정 ───────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
LOG_DIR = "logs"

SYSTEM_PROMPT = """너는 정훈의 전담 운동 코치이자 식단 어드바이저야.
매일 운동 내용, 식단, 컨디션을 기록하고 조언해줘.

역할:
- 운동 코치: 오늘 운동 내용 파악, 다음 운동 추천, 무게/세트/횟수 피드백
- 식단 어드바이저: 먹은 것 기록, 다음 끼니 추천, 칼로리/영양 조언
- 일지 관리자: 대화 내용을 바탕으로 하루 요약 정리

항상 한국어로 대화하고, 친근하고 동기부여되는 톤으로 말해줘.
운동이나 식단 관련 정보가 나오면 꼭 메모해두고 요약할 때 포함시켜."""

# ─── 초기화 ──────────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)
anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
notion = NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

conversation_histories = {}

def get_history(user_id: int):
    if user_id not in conversation_histories:
        conversation_histories[user_id] = []
    return conversation_histories[user_id]

def add_to_history(user_id: int, role: str, content: str):
    history = get_history(user_id)
    history.append({"role": role, "content": content})
    if len(history) > 60:
        conversation_histories[user_id] = history[-60:]

async def get_ai_response(user_id: int, user_message: str) -> str:
    add_to_history(user_id, "user", user_message)
    history = get_history(user_id)

    response = anthropic.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=history
    )

    reply = response.content[0].text
    add_to_history(user_id, "assistant", reply)
    return reply

async def generate_daily_summary(user_id: int) -> str:
    history = get_history(user_id)
    if not history:
        return "오늘 대화 내용이 없어요!"

    summary_request = "오늘 하루 대화 내용을 일지 형식으로 요약해줘. 운동 내용, 식단, 컨디션, 다음 계획 순서로 정리해줘."
    messages = history + [{"role": "user", "content": summary_request}]
    response = anthropic.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    return response.content[0].text

async def save_to_file(user_id: int, summary: str):
    today = date.today().isoformat()
    filename = f"{LOG_DIR}/{today}_{user_id}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# 운동 일지 - {today}\n\n")
        f.write(summary)
        f.write(f"\n\n---\n*저장 시각: {datetime.now().strftime('%H:%M:%S')}*\n")
    return filename

async def save_to_notion(summary: str) -> bool:
    if not notion or not NOTION_DATABASE_ID:
        return False
    try:
        today = date.today().isoformat()
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                "title": {
                    "title": [{"text": {"content": f"운동 일지 - {today}"}}]
                },
                "Date": {
                    "date": {"start": today}
                }
            },
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": summary}}]
                }
            }]
        )
        return True
    except Exception as e:
        print(f"Notion 저장 오류: {e}")
        return False

@bot.event
async def on_ready():
    print(f"✅ {bot.user} 봇 실행 중!")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)
    if not message.content.startswith("/"):
        async with message.channel.typing():
            try:
                reply = await get_ai_response(message.author.id, message.content)
                if len(reply) > 1900:
                    chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
                    for chunk in chunks:
                        await message.channel.send(chunk)
                else:
                    await message.channel.send(reply)
            except Exception as e:
                await message.channel.send(f"⚠️ 오류 발생: {str(e)}")

@bot.command(name="저장")
async def save_log(ctx):
    async with ctx.typing():
        summary = await generate_daily_summary(ctx.author.id)
        filename = await save_to_file(ctx.author.id, summary)
        notion_ok = await save_to_notion(summary)

        result = f"📝 **오늘의 운동 일지 저장 완료!**\n\n{summary}\n\n"
        result += f"✅ 파일 저장: `{filename}`\n"
        if notion_ok:
            result += "✅ 노션 저장 완료!\n"

        if len(result) > 1900:
            await ctx.send(result[:1900])
            await ctx.send(result[1900:])
        else:
            await ctx.send(result)

@bot.command(name="초기화")
async def reset_history(ctx):
    conversation_histories[ctx.author.id] = []
    await ctx.send("🔄 대화 히스토리를 초기화했어요!")

@bot.command(name="히스토리")
async def show_history(ctx):
    history = get_history(ctx.author.id)
    turns = len(history) // 2
    await ctx.send(f"📊 현재 대화: {turns}턴")

@bot.command(name="도움말")
async def help_command(ctx):
    help_text = """**🏋️ 운동 일지 봇 사용법**

**일반 대화:** 그냥 채팅하면 돼!
예) "오늘 스쿼트 3세트 100kg 했어"
예) "점심 뭐 먹을까?"
예) "내일 운동 루틴 추천해줘"

**커맨드:**
`/저장` - 오늘 일지 요약 저장
`/초기화` - 대화 히스토리 초기화
`/히스토리` - 현재 대화 턴 수 확인
`/도움말` - 이 메시지"""
    await ctx.send(help_text)

bot.run(DISCORD_TOKEN)
