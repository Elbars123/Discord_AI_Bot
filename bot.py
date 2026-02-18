import os
import discord
from discord.ext import commands
from anthropic import Anthropic
from datetime import datetime, date
from notion_client import Client as NotionClient

# ─── 설정 ───────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
LOG_DIR = "logs"

# ─── 채널별 시스템 프롬프트 ──────────────────────────
SYSTEM_PROMPTS = {
    "운동": """너는 정훈의 전담 운동 코치야.
- 오늘 운동 내용 파악, 다음 운동 추천, 무게/세트/횟수 피드백
- 운동 루틴 설계, 부위별 운동 추천, 부상 예방 조언
- 항상 한국어로 대화하고, 친근하고 동기부여되는 톤으로 말해줘.""",

    "식단": """너는 정훈의 전담 식단 어드바이저야.
- 먹은 것 기록, 다음 끼니 추천, 칼로리/영양 조언
- 다이어트 목표에 맞는 식단 설계, 외식 메뉴 추천
- 콜레스테롤 관리, 단백질 섭취 최적화 등 건강한 식습관 조언
- 항상 한국어로 대화하고, 친근한 톤으로 말해줘.""",

    "번역": """너는 정훈의 전담 번역 어시스턴트야.
- 한국어 ↔ 중국어 ↔ 영어 번역
- 자연스러운 표현으로 번역하고, 필요하면 뉘앙스 설명
- 중국어는 간체자 기준으로 번역하고 병음도 함께 제공
- 항상 한국어로 설명해줘.""",

    "일정": """너는 정훈의 전담 일정 관리 비서야.
- 일정 정리, 우선순위 조언, 시간 관리 도움
- 할 일 목록 정리, 데드라인 관리
- 업무와 개인 일정 균형 조언
- 항상 한국어로 대화하고, 효율적이고 명확하게 답해줘.""",

    "default": """너는 정훈의 만능 AI 비서야.
- 운동, 식단, 번역, 일정, 일반 질문 등 무엇이든 도와줘
- 게임(원신 등), 개발(Unity, 게임 개발), 일상적인 질문 모두 OK
- 친근하고 실용적인 조언을 해줘
- 항상 한국어로 대화해줘."""
}

CHANNEL_MODES = {
    "운동": "운동",
    "식단": "식단",
    "번역": "번역",
    "일정": "일정",
}

# ─── 초기화 ──────────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)
anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
notion = NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

conversation_histories = {}

def get_model(mode: str) -> str:
    if mode == "번역":
        return "claude-sonnet-4-6"
    return "claude-haiku-4-5-20251001"

def get_channel_mode(channel_name: str) -> str:
    for keyword, mode in CHANNEL_MODES.items():
        if keyword in channel_name:
            return mode
    return "default"

def get_history(channel_id: int):
    if channel_id not in conversation_histories:
        conversation_histories[channel_id] = []
    return conversation_histories[channel_id]

def add_to_history(channel_id: int, role: str, content: str):
    history = get_history(channel_id)
    history.append({"role": role, "content": content})
    if len(history) > 60:
        conversation_histories[channel_id] = history[-60:]

async def get_ai_response(channel_id: int, channel_name: str, user_message: str) -> str:
    add_to_history(channel_id, "user", user_message)
    history = get_history(channel_id)
    mode = get_channel_mode(channel_name)
    system_prompt = SYSTEM_PROMPTS[mode]

    response = anthropic.messages.create(
        model=get_model(mode),
        max_tokens=1024,
        system=system_prompt,
        messages=history
    )

    reply = response.content[0].text
    add_to_history(channel_id, "assistant", reply)
    return reply

async def generate_summary(channel_id: int, channel_name: str) -> str:
    history = get_history(channel_id)
    if not history:
        return "대화 내용이 없어요!"

    mode = get_channel_mode(channel_name)
    if mode == "운동":
        summary_request = "오늘 운동 내용을 일지 형식으로 요약해줘. 운동 종목, 세트/횟수, 컨디션, 다음 계획 순서로."
    elif mode == "식단":
        summary_request = "오늘 식단을 일지 형식으로 요약해줘. 끼니별 식사 내용, 칼로리 추정, 개선점 순서로."
    else:
        summary_request = "오늘 대화 내용을 간단히 요약해줘."

    messages = history + [{"role": "user", "content": summary_request}]
    response = anthropic.messages.create(
        model=get_model(mode),
        max_tokens=1024,
        system=SYSTEM_PROMPTS[mode],
        messages=messages
    )
    return response.content[0].text

async def save_to_file(channel_id: int, channel_name: str, summary: str):
    today = date.today().isoformat()
    filename = f"{LOG_DIR}/{today}_{channel_name}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# {channel_name} 일지 - {today}\n\n")
        f.write(summary)
        f.write(f"\n\n---\n*저장 시각: {datetime.now().strftime('%H:%M:%S')}*\n")
    return filename

async def save_to_notion(channel_name: str, summary: str) -> bool:
    if not notion or not NOTION_DATABASE_ID:
        return False
    try:
        today = date.today().isoformat()
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                "title": {
                    "title": [{"text": {"content": f"{channel_name} 일지 - {today}"}}]
                },
                "Date": {"date": {"start": today}}
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
                reply = await get_ai_response(
                    message.channel.id,
                    message.channel.name,
                    message.content
                )
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
        summary = await generate_summary(ctx.channel.id, ctx.channel.name)
        filename = await save_to_file(ctx.channel.id, ctx.channel.name, summary)
        notion_ok = await save_to_notion(ctx.channel.name, summary)

        result = f"📝 **일지 저장 완료!**\n\n{summary}\n\n✅ 파일: `{filename}`\n"
        if notion_ok:
            result += "✅ 노션 저장 완료!\n"

        if len(result) > 1900:
            await ctx.send(result[:1900])
            await ctx.send(result[1900:])
        else:
            await ctx.send(result)

@bot.command(name="초기화")
async def reset_history(ctx):
    conversation_histories[ctx.channel.id] = []
    await ctx.send("🔄 이 채널의 대화 히스토리를 초기화했어요!")

@bot.command(name="모드")
async def show_mode(ctx):
    mode = get_channel_mode(ctx.channel.name)
    mode_emoji = {"운동": "🏋️", "식단": "🥗", "번역": "🌏", "일정": "📅", "default": "🤖"}
    await ctx.send(f"{mode_emoji.get(mode, '🤖')} 현재 채널 모드: **{mode}**")

@bot.command(name="도움말")
async def help_command(ctx):
    help_text = """**🤖 AI 비서 봇 사용법**

**채널별 자동 모드:**
`#운동` → 운동 코치 모드 🏋️
`#식단` → 식단 어드바이저 모드 🥗
`#번역` → 번역 모드 🌏
`#일정` → 일정 관리 모드 📅
그 외 채널 → 만능 비서 모드 🤖

**커맨드:**
`/저장` - 오늘 대화 요약 저장
`/초기화` - 이 채널 대화 히스토리 초기화
`/모드` - 현재 채널 모드 확인
`/도움말` - 이 메시지"""
    await ctx.send(help_text)

bot.run(DISCORD_TOKEN)
