import os
import sqlite3
import asyncio
import discord
from discord.ext import commands
from anthropic import AsyncAnthropic
from datetime import datetime, date
from notion_client import AsyncClient as NotionAsyncClient

# ─── 환경변수 유효성 검사 ─────────────────────────────
REQUIRED_ENV_VARS = ["DISCORD_TOKEN", "ANTHROPIC_API_KEY"]
missing_vars = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if missing_vars:
    raise EnvironmentError(
        f"❌ 필수 환경변수가 설정되지 않았습니다: {', '.join(missing_vars)}\n"
        f"   DISCORD_TOKEN, ANTHROPIC_API_KEY 를 환경변수에 등록해주세요."
    )

DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN       = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

LOG_DIR = "logs"
DB_PATH = "history.db"
MAX_HISTORY = 60  # 채널당 보관할 최대 메시지 수

# ─── 모델 설정 ────────────────────────────────────────
MODEL_MAP = {
    "번역":    "claude-3-5-sonnet-20241022",   # 번역은 고품질 모델
    "default": "claude-3-5-haiku-20241022",    # 나머지는 빠른 모델
}

# ─── 채널별 시스템 프롬프트 ──────────────────────────
SYSTEM_PROMPTS = {
    "헬스": """너는 정훈의 전담 헬스 트레이너 겸 식단 어드바이저야.

[운동 코칭]
- 오늘 운동 내용 파악, 다음 운동 추천, 무게/세트/횟수 피드백
- 운동 루틴 설계, 부위별 운동 추천, 부상 예방 조언

[식단 관리]
- 먹은 것 기록, 다음 끼니 추천, 칼로리/영양 조언
- 다이어트 목표에 맞는 식단 설계, 외식 메뉴 추천
- 콜레스테롤 관리, 단백질 섭취 최적화 등 건강한 식습관 조언

항상 한국어로 대화하고, 친근하고 동기부여되는 톤으로 말해줘.""",

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
- 항상 한국어로 대화해줘.""",
}

CHANNEL_MODES = {
    "헬스": "헬스",
    "번역": "번역",
    "일정": "일정",
}

MODE_EMOJI = {
    "헬스":    "💪",
    "번역":    "🌏",
    "일정":    "📅",
    "default": "🤖",
}

# ─── SQLite 히스토리 관리 ─────────────────────────────
def init_db():
    """DB 테이블 초기화 (최초 실행 시 생성)"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel ON conversation_history (channel_id, timestamp)"
        )
        conn.commit()

def _get_history(channel_id: int) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversation_history "
            "WHERE channel_id = ? ORDER BY timestamp",
            (channel_id,)
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]

def _add_message(channel_id: int, role: str, content: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO conversation_history (channel_id, role, content) VALUES (?, ?, ?)",
            (channel_id, role, content)
        )
        # MAX_HISTORY 초과분 제거
        conn.execute("""
            DELETE FROM conversation_history
            WHERE channel_id = ?
              AND id NOT IN (
                  SELECT id FROM conversation_history
                  WHERE channel_id = ?
                  ORDER BY timestamp DESC
                  LIMIT ?
              )
        """, (channel_id, channel_id, MAX_HISTORY))
        conn.commit()

def _clear_history(channel_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM conversation_history WHERE channel_id = ?",
            (channel_id,)
        )
        conn.commit()

def _count_history(channel_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM conversation_history WHERE channel_id = ?",
            (channel_id,)
        ).fetchone()[0]

# asyncio에서 블로킹 DB 작업을 별도 스레드로 실행하는 래퍼
async def get_history(channel_id: int):
    return await asyncio.to_thread(_get_history, channel_id)

async def add_message(channel_id: int, role: str, content: str):
    await asyncio.to_thread(_add_message, channel_id, role, content)

async def clear_history(channel_id: int):
    await asyncio.to_thread(_clear_history, channel_id)

async def count_history(channel_id: int):
    return await asyncio.to_thread(_count_history, channel_id)

# ─── 유틸 함수 ───────────────────────────────────────
def get_model(mode: str) -> str:
    return MODEL_MAP.get(mode, MODEL_MAP["default"])

def get_channel_mode(channel_name: str) -> str:
    for keyword, mode in CHANNEL_MODES.items():
        if keyword in channel_name:
            return mode
    return "default"

async def send_long_message(target, text: str):
    """2000자 초과 메시지를 1900자 청크로 분할 전송"""
    if len(text) <= 1900:
        await target.send(text)
        return
    for i in range(0, len(text), 1900):
        await target.send(text[i:i + 1900])

# ─── AI 응답 ─────────────────────────────────────────
async def get_ai_response(channel_id: int, channel_name: str, user_message: str) -> str:
    await add_message(channel_id, "user", user_message)
    history = await get_history(channel_id)
    mode = get_channel_mode(channel_name)

    response = await anthropic.messages.create(
        model=get_model(mode),
        max_tokens=1024,
        system=SYSTEM_PROMPTS[mode],
        messages=history,
    )
    reply = response.content[0].text
    await add_message(channel_id, "assistant", reply)
    return reply

async def generate_summary(channel_id: int, channel_name: str) -> str:
    history = await get_history(channel_id)
    if not history:
        return "대화 내용이 없어요!"

    mode = get_channel_mode(channel_name)
    summary_prompts = {
        "헬스": (
            "오늘 헬스 기록을 일지 형식으로 요약해줘.\n"
            "1. 운동: 종목, 세트/횟수, 컨디션, 다음 계획\n"
            "2. 식단: 끼니별 식사 내용, 칼로리 추정, 개선점"
        ),
        "일정": "오늘 일정 대화 내용을 정리해줘. 완료한 일, 남은 할일, 내일 계획 순서로.",
    }
    summary_request = summary_prompts.get(mode, "오늘 대화 내용을 간단히 요약해줘.")

    response = await anthropic.messages.create(
        model=get_model(mode),
        max_tokens=1024,
        system=SYSTEM_PROMPTS[mode],
        messages=history + [{"role": "user", "content": summary_request}],
    )
    return response.content[0].text

# ─── 저장 ─────────────────────────────────────────────
def _write_file(filename: str, content: str):
    """파일을 안전하게 with 블록으로 저장 (블로킹 → to_thread로 호출)"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)

async def save_to_file(channel_name: str, summary: str) -> str:
    today = date.today().isoformat()
    filename = f"{LOG_DIR}/{today}_{channel_name}.md"
    content = (
        f"# {channel_name} 일지 - {today}\n\n"
        f"{summary}\n\n"
        f"---\n*저장 시각: {datetime.now().strftime('%H:%M:%S')}*\n"
    )
    await asyncio.to_thread(_write_file, filename, content)
    return filename

async def save_to_notion(channel_name: str, summary: str) -> bool:
    if not notion or not NOTION_DATABASE_ID:
        return False
    try:
        today = date.today().isoformat()
        await notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                "title": {
                    "title": [{"text": {"content": f"{channel_name} 일지 - {today}"}}]
                },
                "Date": {"date": {"start": today}},
            },
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": summary}}]
                },
            }],
        )
        return True
    except Exception as e:
        print(f"[Notion 오류] {e}")
        return False

# ─── 초기화 ───────────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)
init_db()

anthropic = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
notion = NotionAsyncClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ─── 이벤트 ───────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ {bot.user} 봇 실행 중!")
    print(f"📦 연결된 서버 수: {len(bot.guilds)}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # DM 채널은 name 속성이 없으므로 무시
    if not isinstance(message.channel, discord.TextChannel):
        return
    await bot.process_commands(message)
    if not message.content.startswith("/"):
        async with message.channel.typing():
            try:
                reply = await get_ai_response(
                    message.channel.id,
                    message.channel.name,
                    message.content,
                )
                await send_long_message(message.channel, reply)
            except Exception as e:
                await message.channel.send(f"⚠️ 오류 발생: {e}")

# ─── 커맨드 ───────────────────────────────────────────
@bot.command(name="저장")
async def save_log(ctx):
    """오늘 대화를 요약해 파일 & 노션에 저장합니다."""
    async with ctx.typing():
        summary  = await generate_summary(ctx.channel.id, ctx.channel.name)
        filename = await save_to_file(ctx.channel.name, summary)
        notion_ok = await save_to_notion(ctx.channel.name, summary)

        result = f"📝 **일지 저장 완료!**\n\n{summary}\n\n✅ 파일: `{filename}`\n"
        if notion_ok:
            result += "✅ 노션 저장 완료!\n"

        await send_long_message(ctx, result)

@bot.command(name="초기화")
async def reset_history(ctx):
    """이 채널의 대화 히스토리를 삭제합니다."""
    await clear_history(ctx.channel.id)
    await ctx.send("🔄 이 채널의 대화 히스토리를 초기화했어요!")

@bot.command(name="히스토리")
async def show_history(ctx):
    """현재 채널의 저장된 대화 수를 보여줍니다."""
    count = await count_history(ctx.channel.id)
    turns = count // 2
    await ctx.send(
        f"💬 현재 저장된 대화: **{count}개** 메시지 (약 **{turns}턴**)\n"
        f"⚙️ 최대 보관: {MAX_HISTORY}개"
    )

@bot.command(name="모드")
async def show_mode(ctx):
    """현재 채널의 AI 모드를 확인합니다."""
    mode = get_channel_mode(ctx.channel.name)
    emoji = MODE_EMOJI.get(mode, "🤖")
    await ctx.send(f"{emoji} 현재 채널 모드: **{mode}**\n🧠 사용 모델: `{get_model(mode)}`")

@bot.command(name="도움말")
async def help_command(ctx):
    """봇 사용법을 출력합니다."""
    help_text = """**🤖 AI 비서 봇 사용법**

**채널별 자동 모드:**
`#헬스` → 운동 코치 + 식단 어드바이저 모드 💪
`#번역` → 번역 모드 🌏 (고품질 Sonnet 모델)
`#일정` → 일정 관리 모드 📅
그 외 채널 → 만능 비서 모드 🤖

**커맨드:**
`/저장` — 오늘 대화 AI 요약 후 파일 & 노션 저장
`/초기화` — 이 채널 대화 히스토리 삭제
`/히스토리` — 현재 저장된 대화 수 확인
`/모드` — 현재 채널 모드 및 사용 모델 확인
`/도움말` — 이 메시지"""
    await ctx.send(help_text)

bot.run(DISCORD_TOKEN)
