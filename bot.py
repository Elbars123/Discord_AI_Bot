import os
import re
import sqlite3
import asyncio
import json
import discord
from discord.ext import commands
from anthropic import AsyncAnthropic
from datetime import datetime, date, timedelta
from notion_client import AsyncClient as NotionAsyncClient
from dotenv import load_dotenv

# Google Calendar (선택 의존성)
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as google_build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

load_dotenv()  # 로컬 .env 파일 로드

# ─── 환경변수 유효성 검사 ─────────────────────────────
REQUIRED_ENV_VARS = ["DISCORD_TOKEN", "ANTHROPIC_API_KEY"]
missing_vars = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if missing_vars:
    raise EnvironmentError(
        f"❌ 필수 환경변수 누락: {', '.join(missing_vars)}\n"
        f"   .env 파일 또는 Railway Variables에 등록해주세요."
    )

DISCORD_TOKEN     = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# 노션
NOTION_TOKEN             = os.environ.get("NOTION_TOKEN", "")
NOTION_HEALTH_DB_ID      = os.environ.get("NOTION_HEALTH_DB_ID", "")
NOTION_TODO_DB_ID        = os.environ.get("NOTION_TODO_DB_ID", "")
NOTION_TRANSLATION_DB_ID = os.environ.get("NOTION_TRANSLATION_DB_ID", "")
NOTION_MEMO_DB_ID        = os.environ.get("NOTION_MEMO_DB_ID", "")

# 구글 캘린더
GOOGLE_CALENDAR_ID      = os.environ.get("GOOGLE_CALENDAR_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

DB_PATH      = "history.db"
MAX_HISTORY  = 60
MAX_MSG_LEN  = 1000   # 입력 메시지 최대 길이 (초과 시 잘라냄)
COOLDOWN_SEC = 5      # 유저당 최소 요청 간격 (초)

# 레이트 리밋: {user_id: last_request_time}
_last_request: dict[int, float] = {}

# ─── 모델 설정 ────────────────────────────────────────
MODEL_MAP = {
    "번역":    "claude-sonnet-4-6",
    "default": "claude-haiku-4-5-20251001",
}

# ─── 채널별 시스템 프롬프트 ──────────────────────────
SYSTEM_PROMPTS = {
    "헬스": """너는 정훈의 전담 헬스 트레이너 겸 식단 어드바이저야. Notion과 연동되어 있어서 오늘 대화를 일지로 저장할 수 있어.

[운동 코칭]
- 오늘 운동 내용 파악, 다음 운동 추천, 무게/세트/횟수 피드백
- 운동 루틴 설계, 부위별 운동 추천, 부상 예방 조언

[식단 관리]
- 먹은 것 기록, 다음 끼니 추천, 칼로리/영양 조언
- 다이어트 목표에 맞는 식단 설계, 외식 메뉴 추천
- 콜레스테롤 관리, 단백질 섭취 최적화 등 건강한 식습관 조언

[사용 가능한 커맨드]
- `/저장` — 오늘 대화를 AI가 요약해서 파일과 Notion 헬스 일지 DB에 자동 저장
- `/초기화` — 이 채널 대화 히스토리 삭제
- `/히스토리` — 현재 저장된 대화 수 확인

🚫 절대 금지 사항 (이것만큼은 반드시 지켜):
- "저장 중...", "기록 중...", "삭제 중...", "처리 중..." 같은 말 절대 금지 — 너는 실시간으로 아무것도 못 해
- "저장했어요", "기록했어요", "삭제했어요" 같은 말 절대 금지 — 실제로 한 게 아니니까
- 사용자가 "저장해줘", "삭제해줘", "정리해줘" 라고 하면 → 해당 커맨드(/저장, /초기화 등)를 안내만 해줘
- 너는 Notion, 파일, 메모리에 직접 접근하는 능력이 없어. 커맨드를 통해서만 가능해
- 기록 수정은 `/헬스수정 날짜 | 내용` 커맨드로만 가능해
항상 한국어로 대화하고, 친근하고 동기부여되는 톤으로 말해줘.""",

    "번역": """너는 정훈의 전담 번역 어시스턴트야. Notion과 연동되어 있어서 번역할 때마다 자동으로 번역 기록 DB에 저장돼.

[번역 기능]
- 한국어 ↔ 중국어 ↔ 영어 번역
- 자연스러운 표현으로 번역하고, 필요하면 뉘앙스 설명
- 중국어는 간체자 기준으로 번역하고 병음도 함께 제공

[Notion 자동 저장]
- 번역할 때마다 원문과 번역 결과가 Notion 번역 기록 DB에 자동으로 저장돼
- 별도 커맨드 없이 대화만 해도 자동 저장됨

사용자가 "번역 기록 저장돼?" 같이 물으면 "네! 번역할 때마다 Notion에 자동으로 저장되고 있어요 🌏" 라고 안내해줘.
항상 한국어로 설명해줘.""",

    "일정": """너는 정훈의 전담 일정 관리 비서야. 구글 캘린더와 실제로 연동되어 있어.

[캘린더 연동 기능]
- `/일정추가 [내용]` — 자연어로 일정을 파싱해서 구글 캘린더에 자동 추가 (예: /일정추가 내일 오후 3시 치과)
- `/오늘일정` — 오늘 구글 캘린더에 등록된 일정 조회
- `/이번주일정` — 이번 주 일정 전체 조회
- 일정 관련 메시지를 보내면 자동으로 캘린더 추가도 시도해줘

[할일 관리 (Notion 연동)]
- `/할일추가 [내용]` — Notion 할일 DB에 추가
- `/할일목록` — 미완료 할일 목록 조회
- `/할일완료 [이름]` — 완료 처리

[일반 조언]
- 일정 우선순위 조언, 시간 관리 도움, 데드라인 관리
- 업무와 개인 일정 균형 조언

사용자가 "캘린더 연결됐어?" 같이 물어보면 "네, 구글 캘린더와 연동되어 있어요! `/오늘일정` 이나 `/일정추가`를 써보세요 📅" 라고 안내해줘.
🚫 절대 금지 사항 (반드시 지켜):
- "저장 중...", "추가 중...", "삭제 중...", "처리 중..." 같은 말 절대 금지
- "저장했어요", "추가했어요", "삭제했어요" 같은 말 절대 금지 — 실제로 한 게 아니야
- 사용자가 "저장해줘", "추가해줘", "삭제해줘" 라고 하면 → 해당 커맨드를 안내만 해줘
- 할일 수정은 `/할일수정 기존이름 | 새이름` 커맨드로만 가능해. 사용자가 "수정해줘"라고 하면 안내해줘
항상 한국어로 대화하고, 효율적이고 명확하게 답해줘.""",

    "default": """너는 정훈의 만능 AI 비서야. 구글 캘린더 및 Notion과 연동되어 있어.

[대화 가능 주제]
- 운동, 식단, 번역, 일정, 일반 질문 등 무엇이든 도와줘
- 게임(원신 등), 개발(Unity, 게임 개발), 일상적인 질문 모두 OK
- 친근하고 실용적인 조언을 해줘

[사용 가능한 커맨드 — 채널 어디서나 동작]
- `/저장` — 오늘 대화 AI 요약 후 파일 & Notion 저장
- `/초기화` — 이 채널 대화 히스토리 삭제
- `/히스토리` — 현재 저장된 대화 수 확인
- `/모드` — 현재 채널 모드 및 사용 AI 모델 확인
- `/도움말` — 전체 커맨드 목록 출력

[일정 관련 커맨드 (구글 캘린더 연동)]
- `/일정추가 [내용]` — 자연어로 일정 파싱 후 캘린더에 추가
- `/오늘일정` — 오늘 구글 캘린더 일정 조회
- `/이번주일정` — 이번 주 일정 조회

[할일 & 메모 커맨드 (Notion 연동)]
- `/할일추가 [내용]` — Notion 할일 DB에 추가
- `/할일목록` — 미완료 할일 목록 조회
- `/할일완료 [이름]` — 완료 처리
- `/할일수정 [이름] | [새이름 또는 마감:날짜 또는 우선순위:높음]` — 할일 수정
- `/메모 [제목] | [내용]` — Notion 메모 DB에 저장
- `/메모수정 [제목] | [새 내용]` — 메모 내용 수정

[채널별 특화 기능]
- `#헬스` — 운동/식단 특화 + `/저장` 시 Notion 헬스 일지 저장
- `#번역` — 번역 특화(Sonnet 모델) + 번역마다 Notion 자동 저장
- `#일정` — 일정 특화 + 자연어 일정 캘린더 자동 감지

🚫 절대 금지 사항 (반드시 지켜):
- "저장 중...", "처리 중...", "삭제 중...", "추가 중..." 같은 진행형 표현 절대 금지
- "저장했어요", "추가했어요", "삭제했어요", "수정했어요" 같은 완료형 표현 절대 금지
- 너는 Notion, 캘린더, 파일에 직접 접근 불가능해. 커맨드를 통해서만 실제 저장/수정이 이루어져
- 사용자가 저장/수정/삭제를 요청하면 → 해당 커맨드(/저장, /메모, /할일추가 등)를 안내해줘
- "메모리에서 삭제할게", "기억에서 지울게" 같은 말도 금지 — 대화 히스토리는 /초기화 커맨드로만 삭제 가능해
사용자가 뭘 할 수 있는지 물어보면 위의 커맨드 목록을 친절하게 안내해줘.
항상 한국어로 대화해줘.""",
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

# ─── SQLite 히스토리 ──────────────────────────────────
def init_db():
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
            "CREATE INDEX IF NOT EXISTS idx_channel "
            "ON conversation_history (channel_id, timestamp)"
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

def _get_today_history(channel_id: int) -> list[dict]:
    """오늘 날짜의 대화만 가져오기 (요약/저장 시 사용)"""
    today = date.today().isoformat()  # "2026-02-18"
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversation_history "
            "WHERE channel_id = ? AND DATE(timestamp) = ? ORDER BY timestamp",
            (channel_id, today)
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]

def _add_message(channel_id: int, role: str, content: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO conversation_history (channel_id, role, content) VALUES (?, ?, ?)",
            (channel_id, role, content)
        )
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
        conn.execute("DELETE FROM conversation_history WHERE channel_id = ?", (channel_id,))
        conn.commit()

def _count_history(channel_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM conversation_history WHERE channel_id = ?",
            (channel_id,)
        ).fetchone()[0]

async def get_history(channel_id: int):
    return await asyncio.to_thread(_get_history, channel_id)

async def get_today_history(channel_id: int):
    return await asyncio.to_thread(_get_today_history, channel_id)

async def add_message(channel_id: int, role: str, content: str):
    await asyncio.to_thread(_add_message, channel_id, role, content)

async def clear_history(channel_id: int):
    await asyncio.to_thread(_clear_history, channel_id)

async def count_history(channel_id: int) -> int:
    return await asyncio.to_thread(_count_history, channel_id)

# ─── 유틸 ────────────────────────────────────────────
def get_model(mode: str) -> str:
    return MODEL_MAP.get(mode, MODEL_MAP["default"])

def get_channel_mode(channel_name: str) -> str:
    for keyword, mode in CHANNEL_MODES.items():
        if keyword in channel_name:
            return mode
    return "default"

async def send_long_message(target, text: str):
    """2000자 초과 메시지 분할 전송"""
    if len(text) <= 1900:
        await target.send(text)
        return
    for i in range(0, len(text), 1900):
        await target.send(text[i:i + 1900])

def _rich_text(text: str) -> list:
    """Notion rich_text 블록 생성 (2000자 제한 대응)"""
    return [{"type": "text", "text": {"content": text[:2000]}}]

# 헬스 기록 자동 불러오기 키워드
HEALTH_LOAD_KEYWORDS = (
    "기록", "불러와", "불러오", "최근", "지난", "저번",
    "얼마나", "뭐했", "어떻게 했", "운동 현황", "식단 현황",
    "진행 상황", "돌아봐", "정리해줘", "보여줘", "확인해줘"
)

async def notion_get_health_logs(days: int = 7) -> str:
    """Notion 헬스 일지 DB에서 최근 N일 기록 조회 후 텍스트로 반환"""
    if not notion or not NOTION_HEALTH_DB_ID:
        return ""
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        res = await notion.databases.query(
            database_id=NOTION_HEALTH_DB_ID,
            filter={"property": "날짜", "date": {"on_or_after": since}},
            sorts=[{"property": "날짜", "direction": "ascending"}],
        )
        if not res["results"]:
            return ""
        logs = []
        for page in res["results"]:
            date_obj  = page["properties"].get("날짜", {}).get("date") or {}
            log_date  = date_obj.get("start", "날짜미상")
            # 페이지 본문 블록 가져오기
            blocks = await notion.blocks.children.list(block_id=page["id"])
            parts  = []
            for block in blocks["results"]:
                btype = block.get("type", "")
                rich  = block.get(btype, {}).get("rich_text", [])
                text  = "".join(r["text"]["content"] for r in rich)
                if text.strip():
                    parts.append(text)
            content = "\n".join(parts) if parts else "내용 없음"
            logs.append(f"[{log_date}]\n{content}")
        return "\n\n---\n\n".join(logs)
    except Exception as e:
        print(f"[Notion 헬스 기록 조회 오류] {e}")
        return ""

# ─── Notion: 헬스 일지 (날짜별 자동 분리 저장) ──────
async def notion_bulk_save_health_logs(text: str) -> tuple[int, list[str]]:
    """
    헬스 요약 텍스트에서 날짜별 섹션을 파싱해 각각 Notion 페이지로 저장.
    반환: (저장된 개수, 저장된 날짜 목록)
    """
    if not notion or not NOTION_HEALTH_DB_ID:
        return 0, []

    # "X월 Y일" 또는 "YYYY-MM-DD" 패턴으로 섹션 분리
    date_re = re.compile(r'(\d{1,2}월\s*\d{1,2}일(?:\s*\([^)]*\))?|\d{4}-\d{2}-\d{2})')
    parts   = date_re.split(text)
    # parts = [before_first_date, date1, content1, date2, content2, ...]

    year   = date.today().year
    saved  = 0
    dates_saved: list[str] = []

    i = 1
    while i + 1 <= len(parts) - 1:
        raw_date = parts[i].strip()
        content  = parts[i + 1].strip()
        i += 2
        if not content:
            continue

        # 날짜 파싱
        try:
            m = re.search(r'(\d{1,2})월\s*(\d{1,2})일', raw_date)
            if m:
                month, day = int(m.group(1)), int(m.group(2))
                log_date   = f"{year}-{month:02d}-{day:02d}"
            else:
                log_date = raw_date  # 이미 YYYY-MM-DD 형식
        except Exception:
            log_date = date.today().isoformat()

        try:
            await notion.pages.create(
                parent={"database_id": NOTION_HEALTH_DB_ID},
                properties={
                    "이름": {"title": [{"text": {"content": f"헬스 일지 - {log_date}"}}]},
                    "날짜": {"date": {"start": log_date}},
                },
                children=[{
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": _rich_text(f"[{raw_date}]\n{content}")},
                }]
            )
            saved += 1
            dates_saved.append(log_date)
        except Exception as e:
            print(f"[Notion 헬스 일지 저장 오류] {log_date}: {e}")

    # 날짜 섹션이 없으면 오늘 날짜로 통째로 저장
    if saved == 0 and text.strip():
        today = date.today().isoformat()
        try:
            await notion.pages.create(
                parent={"database_id": NOTION_HEALTH_DB_ID},
                properties={
                    "이름": {"title": [{"text": {"content": f"헬스 일지 - {today}"}}]},
                    "날짜": {"date": {"start": today}},
                },
                children=[{
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": _rich_text(text)},
                }]
            )
            saved = 1
            dates_saved.append(today)
        except Exception as e:
            print(f"[Notion 헬스 일지 저장 오류] {e}")

    return saved, dates_saved

# ─── Notion: 헬스 일지 JSON 기반 저장 ───────────────────
async def extract_health_json(history: list[dict]) -> list[dict]:
    """
    대화 히스토리에서 헬스 기록을 JSON 배열로 추출 (Haiku + temperature=0).
    반환: [{"date":"YYYY-MM-DD","breakfast":"","lunch":"","dinner":"","snack":"","workout":"","notes":""}]
    """
    today_str = date.today().isoformat()
    conv_text = "\n".join(
        f"{'사용자' if m['role']=='user' else '봇'}: {m['content']}"
        for m in history
    )
    prompt = (
        f"아래 대화에서 사용자가 직접 말한 운동/식단 기록만 추출해줘.\n"
        f"봇의 안내 메시지, 질문 템플릿, '기록이 없네요' 같은 봇 응답은 완전히 무시해.\n"
        f"없는 내용은 절대 지어내지 마. 언급이 없으면 빈 문자열(\"\")로 남겨.\n"
        f"날짜가 명시되지 않으면 오늘({today_str})로 설정해.\n"
        f"요일은 날짜에 맞게 계산해줘 (월/화/수/목/금/토/일).\n"
        f"특기사항은 배열로, 항목별로 분리해줘.\n\n"
        f"반드시 아래 JSON 배열 형식으로만 답해. 다른 설명 없이 JSON만:\n"
        f'[{{"date":"YYYY-MM-DD","weekday":"월","workout_part":"","workout_weight":"","workout_time":"","condition":"","breakfast":"","lunch":"","dinner":"","snack":"","notes":[]}}]\n\n'
        f"[대화]\n{conv_text}"
    )
    try:
        response = await anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            temperature=0,
            system="너는 대화에서 헬스 데이터를 정확히 추출하는 파서야. JSON 배열만 반환해.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        entries = json.loads(raw)
        return entries if isinstance(entries, list) else []
    except Exception as e:
        print(f"[헬스 JSON 추출 오류] {e}")
        return []

def format_health_entry(entry: dict) -> str:
    """JSON 딕셔너리 → 고정 포맷 문자열 (코드에서 포맷 결정, AI 관여 없음)"""
    try:
        d = entry.get("date", date.today().isoformat())
        year, month, day = d.split("-")
        date_label = f"{int(month)}월 {int(day)}일"
    except Exception:
        date_label = entry.get("date", "날짜미상")

    weekday = entry.get("weekday", "")
    header  = f"🗓️ {date_label} ({weekday})" if weekday else f"🗓️ {date_label}"

    def val(k):
        v = entry.get(k, "")
        if isinstance(v, str):
            v = v.strip()
        return v if v else ""

    lines = [
        header,
        "",
        "🏋️ 운동 기록",
        f"운동 부위/종목: {val('workout_part')}",
        f"무게/세트/횟수: {val('workout_weight')}",
        f"운동 시간대: {val('workout_time')}",
        f"컨디션: {val('condition')}",
        "",
        "🍽️ 식단 기록",
        f"아침: {val('breakfast')}",
        f"점심: {val('lunch')}",
        f"저녁: {val('dinner')}",
        f"간식/야식: {val('snack')}",
        "",
        "⭐ 특기사항",
    ]
    notes = entry.get("notes", [])
    if isinstance(notes, list) and notes:
        for note in notes:
            if note.strip():
                lines.append(f"- {note.strip()}")
    elif isinstance(notes, str) and notes.strip():
        lines.append(f"- {notes.strip()}")
    else:
        lines.append("-")
    return "\n".join(lines)

async def notion_save_health_from_json(entries: list[dict]) -> tuple[int, list[str]]:
    """JSON 엔트리 리스트를 Notion 헬스 DB에 저장. 반환: (저장 수, 날짜 목록)"""
    if not notion or not NOTION_HEALTH_DB_ID:
        return 0, []
    saved = 0
    dates_saved = []
    for entry in entries:
        log_date = entry.get("date", date.today().isoformat())
        content  = format_health_entry(entry)
        try:
            await notion.pages.create(
                parent={"database_id": NOTION_HEALTH_DB_ID},
                properties={
                    "이름": {"title": [{"text": {"content": f"헬스 일지 - {log_date}"}}]},
                    "날짜": {"date": {"start": log_date}},
                    "내용": {"rich_text": _rich_text(content)},
                },
                children=[{
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": _rich_text(content)},
                }]
            )
            saved += 1
            dates_saved.append(log_date)
        except Exception as e:
            print(f"[Notion 헬스 일지 저장 오류] {log_date}: {e}")
    return saved, dates_saved

# ─── Notion: 할일 ─────────────────────────────────────
async def notion_add_todo(title: str, due_date: str = "", priority: str = "중간") -> bool:
    """할일을 Notion DB에 추가 (프로퍼티: 이름/마감일/완료/우선순위)"""
    if not notion or not NOTION_TODO_DB_ID:
        return False
    try:
        props = {
            "이름":     {"title": [{"text": {"content": title}}]},
            "완료":     {"checkbox": False},
            "우선순위": {"select": {"name": priority}},
        }
        if due_date:
            props["마감일"] = {"date": {"start": due_date}}
        await notion.pages.create(
            parent={"database_id": NOTION_TODO_DB_ID},
            properties=props
        )
        return True
    except Exception as e:
        print(f"[Notion 할일 추가 오류] {e}")
        return False

async def notion_get_todos() -> list[dict]:
    """Notion DB에서 미완료 할일 조회"""
    if not notion or not NOTION_TODO_DB_ID:
        return []
    try:
        res = await notion.databases.query(
            database_id=NOTION_TODO_DB_ID,
            filter={"property": "완료", "checkbox": {"equals": False}},
            sorts=[{"property": "마감일", "direction": "ascending"}]
        )
        todos = []
        for page in res["results"]:
            props = page["properties"]
            title_arr = props.get("이름", {}).get("title", [])
            title     = title_arr[0]["text"]["content"] if title_arr else "제목없음"
            due_obj   = props.get("마감일", {}).get("date") or {}
            due       = due_obj.get("start", "")
            pri_obj   = props.get("우선순위", {}).get("select") or {}
            priority  = pri_obj.get("name", "")
            todos.append({"id": page["id"], "title": title, "due": due, "priority": priority})
        return todos
    except Exception as e:
        print(f"[Notion 할일 조회 오류] {e}")
        return []

async def notion_complete_todo(title: str) -> bool:
    """할일 이름으로 검색해 완료 처리"""
    if not notion or not NOTION_TODO_DB_ID:
        return False
    try:
        res = await notion.databases.query(
            database_id=NOTION_TODO_DB_ID,
            filter={
                "and": [
                    {"property": "완료", "checkbox": {"equals": False}},
                    {"property": "이름", "title": {"contains": title}},
                ]
            }
        )
        if not res["results"]:
            return False
        page_id = res["results"][0]["id"]
        await notion.pages.update(
            page_id=page_id,
            properties={"완료": {"checkbox": True}}
        )
        return True
    except Exception as e:
        print(f"[Notion 할일 완료 오류] {e}")
        return False

# ─── Notion: 번역 기록 ────────────────────────────────
async def notion_save_translation(original: str, translated: str) -> bool:
    """번역 결과를 Notion DB에 자동 저장 (프로퍼티: 원문/번역/날짜)"""
    if not notion or not NOTION_TRANSLATION_DB_ID:
        return False
    try:
        today = date.today().isoformat()
        await notion.pages.create(
            parent={"database_id": NOTION_TRANSLATION_DB_ID},
            properties={
                "원문": {"title": [{"text": {"content": original[:100]}}]},
                "번역": {"rich_text": _rich_text(translated)},
                "날짜": {"date": {"start": today}},
            },
            children=[
                {"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": _rich_text(f"[원문]\n{original}")}},
                {"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": _rich_text(f"[번역]\n{translated}")}},
            ]
        )
        return True
    except Exception as e:
        print(f"[Notion 번역 저장 오류] {e}")
        return False

# ─── Notion: 메모 ─────────────────────────────────────
async def notion_save_memo(title: str, content: str) -> bool:
    """메모를 Notion DB에 저장 (프로퍼티: 제목/내용/날짜)"""
    if not notion or not NOTION_MEMO_DB_ID:
        return False
    try:
        today = date.today().isoformat()
        await notion.pages.create(
            parent={"database_id": NOTION_MEMO_DB_ID},
            properties={
                "제목": {"title": [{"text": {"content": title}}]},
                "내용": {"rich_text": _rich_text(content)},
                "날짜": {"date": {"start": today}},
            }
        )
        return True
    except Exception as e:
        print(f"[Notion 메모 저장 오류] {e}")
        return False

# ─── Notion: 수정 함수들 ──────────────────────────────
async def notion_update_todo(old_title: str, new_title: str = "", due_date: str = "", priority: str = "") -> bool:
    """할일 이름으로 검색해 내용 수정"""
    if not notion or not NOTION_TODO_DB_ID:
        return False
    try:
        res = await notion.databases.query(
            database_id=NOTION_TODO_DB_ID,
            filter={"property": "이름", "title": {"contains": old_title}}
        )
        if not res["results"]:
            return False
        page_id = res["results"][0]["id"]
        props = {}
        if new_title:
            props["이름"] = {"title": [{"text": {"content": new_title}}]}
        if due_date:
            props["마감일"] = {"date": {"start": due_date}}
        if priority:
            props["우선순위"] = {"select": {"name": priority}}
        if not props:
            return False
        await notion.pages.update(page_id=page_id, properties=props)
        return True
    except Exception as e:
        print(f"[Notion 할일 수정 오류] {e}")
        return False

async def notion_update_memo(title: str, new_title: str = "", new_content: str = "") -> bool:
    """메모 제목으로 검색해 내용 수정"""
    if not notion or not NOTION_MEMO_DB_ID:
        return False
    try:
        res = await notion.databases.query(
            database_id=NOTION_MEMO_DB_ID,
            filter={"property": "제목", "title": {"contains": title}}
        )
        if not res["results"]:
            return False
        page_id = res["results"][0]["id"]
        props = {}
        if new_title:
            props["제목"] = {"title": [{"text": {"content": new_title}}]}
        if new_content:
            props["내용"] = {"rich_text": _rich_text(new_content)}
        if not props:
            return False
        await notion.pages.update(page_id=page_id, properties=props)
        return True
    except Exception as e:
        print(f"[Notion 메모 수정 오류] {e}")
        return False

async def notion_update_health_log(date_str: str, new_content: str) -> bool:
    """날짜로 헬스 기록 검색해 내용 수정"""
    if not notion or not NOTION_HEALTH_DB_ID:
        return False
    try:
        res = await notion.databases.query(
            database_id=NOTION_HEALTH_DB_ID,
            filter={"property": "날짜", "date": {"equals": date_str}}
        )
        if not res["results"]:
            return False
        page_id = res["results"][0]["id"]
        await notion.pages.update(
            page_id=page_id,
            properties={"내용": {"rich_text": _rich_text(new_content)}}
        )
        return True
    except Exception as e:
        print(f"[Notion 헬스 기록 수정 오류] {e}")
        return False

# ─── Google Calendar ──────────────────────────────────
def _get_calendar_service():
    if not GOOGLE_AVAILABLE or not GOOGLE_CREDENTIALS_JSON or not GOOGLE_CALENDAR_ID:
        return None
    try:
        creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return google_build("calendar", "v3", credentials=creds)
    except Exception as e:
        print(f"[Google Calendar 서비스 오류] {e}")
        return None

def _add_event_sync(title: str, start_dt: str, end_dt: str, description: str = "") -> bool:
    service = _get_calendar_service()
    if not service:
        return False
    event = {
        "summary":     title,
        "description": description,
        "start": {"dateTime": start_dt, "timeZone": "Asia/Seoul"},
        "end":   {"dateTime": end_dt,   "timeZone": "Asia/Seoul"},
    }
    service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return True

def _get_events_sync(time_min: str, time_max: str) -> list[dict]:
    service = _get_calendar_service()
    if not service:
        return []
    result = service.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime"
    ).execute()
    events = []
    for e in result.get("items", []):
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        events.append({"title": e.get("summary", "제목없음"), "start": start})
    return events

async def calendar_add_event(title: str, start_dt: str, end_dt: str, description: str = "") -> bool:
    try:
        return await asyncio.to_thread(_add_event_sync, title, start_dt, end_dt, description)
    except Exception as e:
        print(f"[Google Calendar 추가 오류] {e}")
        return False

async def calendar_get_events(time_min: str, time_max: str) -> list[dict]:
    try:
        return await asyncio.to_thread(_get_events_sync, time_min, time_max)
    except Exception as e:
        print(f"[Google Calendar 조회 오류] {e}")
        return []

async def parse_event_from_ai(text: str) -> dict | None:
    """Claude로 자연어 → 일정 정보(JSON) 추출"""
    today_str = date.today().isoformat()
    try:
        response = await anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=f"""너는 일정 파싱 전문가야. 오늘 날짜는 {today_str}이야.
사용자 메시지에서 캘린더에 추가할 일정 정보를 추출해서 아래 JSON 형식으로만 답해줘.
일정 정보가 없으면 {{"has_event": false}} 로만 답해줘.
{{
  "has_event": true,
  "title": "일정 제목",
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM",
  "end_time": "HH:MM",
  "description": ""
}}
end_time이 불명확하면 start_time + 1시간으로 설정해줘.""",
            messages=[{"role": "user", "content": text}]
        )
        raw = response.content[0].text.strip()
        # ```json ... ``` 형식 대응
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        return data if data.get("has_event") else None
    except Exception as e:
        print(f"[일정 파싱 오류] {e}")
        return None

# ─── AI 응답 ─────────────────────────────────────────
async def get_ai_response(channel_id: int, channel_name: str, user_message: str) -> str:
    await add_message(channel_id, "user", user_message)
    history = await get_history(channel_id)
    mode    = get_channel_mode(channel_name)

    response = await anthropic.messages.create(
        model=get_model(mode),
        max_tokens=2048,
        system=SYSTEM_PROMPTS[mode],
        messages=history,
    )
    reply = response.content[0].text
    await add_message(channel_id, "assistant", reply)

    # 번역 채널: 실제 번역 결과일 때만 Notion 저장
    # (병음·한자·알파벳 등 번역 결과 특징적인 문자가 포함된 경우만)
    if mode == "번역" and notion and NOTION_TRANSLATION_DB_ID:
        import unicodedata
        has_cjk    = any(unicodedata.category(c) in ("Lo",) and '\u4e00' <= c <= '\u9fff' for c in reply)
        has_latin  = any(c.isascii() and c.isalpha() for c in reply)
        has_korean = any('\uAC00' <= c <= '\uD7A3' for c in user_message)
        # 원문에 한국어/영어/중국어 있고, 응답에 다른 언어 번역 결과가 보이면 저장
        is_translation = (has_cjk or has_latin) and len(user_message.strip()) >= 2
        if is_translation:
            asyncio.create_task(notion_save_translation(user_message, reply))

    return reply

SUMMARY_TRIGGER_KEYWORDS = (
    "요약", "정리", "포맷", "일지", "저장해줘", "기록해줘", "오늘 어땠", "오늘 뭐했"
)

async def generate_summary(channel_id: int, channel_name: str) -> str:
    # ── 오늘 대화만 가져오기 (이전 날 데이터 포함 방지) ──
    history = await get_today_history(channel_id)
    if not history:
        return "오늘 대화 내용이 없어요! (어제 이전 기록은 `/저장`으로 저장되지 않아요)"
    mode = get_channel_mode(channel_name)

    # ── 핵심 로직: 대화 중에 이미 요약이 나왔으면 그걸 그대로 사용 ──
    # 마지막 2개 메시지가 [user: 요약 요청] → [assistant: 요약 응답] 패턴이면
    # Claude를 다시 호출하지 않고 그 응답을 바로 저장
    if len(history) >= 2:
        last_user = history[-2] if history[-2]["role"] == "user" else None
        last_asst = history[-1] if history[-1]["role"] == "assistant" else None
        if (last_user and last_asst
                and any(kw in last_user["content"] for kw in SUMMARY_TRIGGER_KEYWORDS)
                and len(last_asst["content"]) > 150):
            return last_asst["content"]  # 이미 나온 요약 재사용, Claude 재호출 없음

    # ── 헬스 채널: JSON 추출 후 코드에서 고정 포맷 생성 ──
    if mode == "헬스":
        entries = await extract_health_json(history)
        if not entries:
            return "오늘 기록된 헬스 데이터가 없어요."
        return "\n\n---\n\n".join(format_health_entry(e) for e in entries)

    # ── 그 외 채널: 텍스트 요약 ──
    today_str = date.today().strftime("%Y년 %m월 %d일")
    summary_prompts = {
        "일정": f"오늘({today_str}) 일정 대화 내용을 정리해줘. 완료한 일, 남은 할일, 내일 계획 순서로. 없는 내용은 지어내지 마.",
    }
    summary_request = summary_prompts.get(mode, f"오늘({today_str}) 대화 내용을 간단히 요약해줘. 없는 내용은 절대 지어내지 마.")

    response = await anthropic.messages.create(
        model=get_model(mode),
        max_tokens=2048,
        temperature=0,
        system=SYSTEM_PROMPTS[mode],
        messages=history + [{"role": "user", "content": summary_request}],
    )
    return response.content[0].text

# ─── 초기화 ───────────────────────────────────────────
init_db()

anthropic = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
notion    = NotionAsyncClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ─── 이벤트 ───────────────────────────────────────────
@bot.event
async def on_ready():
    notion_status = "✅" if notion else "❌ (NOTION_TOKEN 미설정)"

    # 구글 캘린더: 실제 연결 테스트
    if not GOOGLE_CALENDAR_ID or not GOOGLE_CREDENTIALS_JSON:
        gcal_status = "❌ (GOOGLE_CALENDAR_ID 또는 GOOGLE_CREDENTIALS_JSON 미설정)"
    else:
        gcal_svc = await asyncio.to_thread(_get_calendar_service)
        gcal_status = "✅" if gcal_svc else "❌ (JSON 파싱 오류 또는 권한 문제 — Railway 로그 확인)"

    print(f"✅ {bot.user} 봇 실행 중!")
    print(f"📦 연결된 서버 수: {len(bot.guilds)}")
    print(f"📓 Notion:           {notion_status}")
    print(f"📅 Google Calendar:  {gcal_status}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # DM 채널 무시 (name 속성 없음)
    if not isinstance(message.channel, discord.TextChannel):
        return
    await bot.process_commands(message)
    if not message.content.startswith("/"):

        # ── 레이트 리밋 체크 ──────────────────────────────
        import time
        now = time.monotonic()
        last = _last_request.get(message.author.id, 0)
        remaining = COOLDOWN_SEC - (now - last)
        if remaining > 0:
            await message.channel.send(
                f"⏳ {message.author.mention} 너무 빠르게 요청하고 있어요! "
                f"**{remaining:.1f}초** 후에 다시 시도해주세요.",
                delete_after=remaining + 1
            )
            return
        _last_request[message.author.id] = now

        # ── 메시지 길이 제한 ──────────────────────────────
        user_text = message.content
        if len(user_text) > MAX_MSG_LEN:
            user_text = user_text[:MAX_MSG_LEN]
            await message.channel.send(
                f"⚠️ 메시지가 너무 길어서 앞 {MAX_MSG_LEN}자만 처리했어요.",
                delete_after=5
            )

        # ── 메모 저장 트리거 감지 (채널 무관, Claude 재호출 없음) ──
        MEMO_TRIGGER = ("메모로 저장", "메모 저장", "메모해줘", "메모로 남겨",
                        "메모 남겨", "메모로 기록", "메모에 저장")
        if any(kw in user_text for kw in MEMO_TRIGGER):
            if not notion or not NOTION_MEMO_DB_ID:
                await message.channel.send("❌ Notion 메모 DB가 설정되지 않았어요. (NOTION_MEMO_DB_ID 확인)")
            else:
                hist = await get_history(message.channel.id)
                last_ai, last_topic = "", ""
                for msg in reversed(hist):
                    if msg["role"] == "assistant" and not last_ai:
                        last_ai = msg["content"]
                    elif msg["role"] == "user" and last_ai and not last_topic:
                        last_topic = msg["content"]
                        break
                if last_ai:
                    title   = last_topic[:50] if last_topic else user_text[:50]
                    content = (f"[질문]\n{last_topic}\n\n[답변]\n{last_ai}"
                               if last_topic else last_ai)
                    ok = await notion_save_memo(title, content)
                    if ok:
                        await message.channel.send(
                            f"📝 **메모 저장 완료!**\n제목: **{title}**"
                        )
                    else:
                        await message.channel.send("❌ 메모 저장 실패")
                else:
                    await message.channel.send(
                        "❌ 저장할 대화 내용이 없어요. 먼저 대화를 해주세요!"
                    )
            return  # Claude 호출 없이 종료

        async with message.channel.typing():
            try:
                # 헬스 채널: 기록 관련 키워드 감지 → Notion에서 자동 불러오기
                injected_text = user_text
                if get_channel_mode(message.channel.name) == "헬스" and notion:
                    if any(kw in user_text for kw in HEALTH_LOAD_KEYWORDS):
                        # "최근 X일" 파싱 (기본 7일)
                        days = 7
                        for num in range(30, 0, -1):
                            if str(num) in user_text:
                                days = num
                                break
                        records = await notion_get_health_logs(days)
                        if records:
                            injected_text = (
                                f"[정훈의 최근 {days}일 헬스 기록 (Notion에서 불러옴)]\n"
                                f"{records}\n\n"
                                f"---\n"
                                f"[정훈의 메시지]\n{user_text}"
                            )
                        else:
                            injected_text = (
                                f"{user_text}\n\n"
                                f"(참고: Notion에 최근 {days}일 헬스 기록이 없어요. "
                                f"`/저장`으로 기록을 먼저 쌓아야 해요!)"
                            )

                reply = await get_ai_response(
                    message.channel.id,
                    message.channel.name,
                    injected_text,
                )
                await send_long_message(message.channel, reply)

                # 일정 채널: 시간 관련 키워드 있을 때만 일정 파싱 (이중 API 호출 방지)
                TIME_KEYWORDS = ("오전", "오후", "시", "분", "내일", "모레", "다음주",
                                 "월요일", "화요일", "수요일", "목요일", "금요일",
                                 "토요일", "일요일", "월", "일", "날")
                if (get_channel_mode(message.channel.name) == "일정"
                        and GOOGLE_CALENDAR_ID
                        and any(kw in user_text for kw in TIME_KEYWORDS)):
                    event = await parse_event_from_ai(user_text)
                    if event:
                        start_dt = f"{event['date']}T{event['start_time']}:00+09:00"
                        end_dt   = f"{event['date']}T{event['end_time']}:00+09:00"
                        ok = await calendar_add_event(
                            event["title"], start_dt, end_dt,
                            event.get("description", "")
                        )
                        if ok:
                            await message.channel.send(
                                f"📅 캘린더에 자동 추가했어요!\n"
                                f"**{event['title']}** — "
                                f"{event['date']} {event['start_time']}~{event['end_time']}"
                            )
            except Exception as e:
                await message.channel.send(f"⚠️ 오류 발생: {e}")

# ─── 기본 커맨드 ──────────────────────────────────────
@bot.command(name="저장")
async def save_log(ctx):
    """오늘 대화를 AI가 요약해 Notion에 저장"""
    async with ctx.typing():
        mode = get_channel_mode(ctx.channel.name)

        if mode == "헬스":
            # ── 헬스: JSON 추출 → 고정 포맷 → Notion 저장 ──
            history = await get_today_history(ctx.channel.id)
            if not history:
                await ctx.send("❌ 오늘 대화 내용이 없어요!")
                return
            entries = await extract_health_json(history)
            if not entries:
                await ctx.send("❌ 저장할 헬스 기록을 찾지 못했어요. 대화에서 운동/식단 내용을 먼저 말해주세요!")
                return
            # 포맷 미리보기
            formatted = "\n\n---\n\n".join(format_health_entry(e) for e in entries)
            # Notion 저장
            count, dates = await notion_save_health_from_json(entries)
            dates_str = ", ".join(dates) if dates else "없음"
            result = (
                f"📝 **헬스 일지 저장 완료!**\n\n"
                f"{formatted}\n\n"
                f"{'✅' if count > 0 else '❌'} Notion **{count}개** 저장 ({dates_str})"
            )
        else:
            # ── 그 외 채널: 텍스트 요약 저장 ──
            summary = await generate_summary(ctx.channel.id, ctx.channel.name)
            result  = f"📝 **일지 저장 완료!**\n\n{summary}"

        await send_long_message(ctx, result)

@bot.command(name="초기화")
async def reset_history(ctx):
    """이 채널의 대화 히스토리를 삭제"""
    await clear_history(ctx.channel.id)
    await ctx.send("🔄 이 채널의 대화 히스토리를 초기화했어요!")

@bot.command(name="히스토리")
async def show_history(ctx):
    """현재 채널의 저장된 대화 수 확인"""
    count = await count_history(ctx.channel.id)
    turns = count // 2
    await ctx.send(
        f"💬 현재 저장된 대화: **{count}개** 메시지 (약 **{turns}턴**)\n"
        f"⚙️ 최대 보관: {MAX_HISTORY}개"
    )

@bot.command(name="모드")
async def show_mode(ctx):
    """현재 채널의 AI 모드 및 사용 모델 확인"""
    mode  = get_channel_mode(ctx.channel.name)
    emoji = MODE_EMOJI.get(mode, "🤖")
    await ctx.send(f"{emoji} 현재 채널 모드: **{mode}**\n🧠 사용 모델: `{get_model(mode)}`")

# ─── 일정 커맨드 ──────────────────────────────────────
@bot.command(name="일정추가")
async def add_schedule(ctx, *, content: str = None):
    """자연어로 구글 캘린더에 일정 추가. 예: /일정추가 내일 오후 3시 치과"""
    if not content:
        await ctx.send("❌ 내용을 입력해주세요.\n예: `/일정추가 내일 오후 3시 치과 예약`")
        return
    if not GOOGLE_CALENDAR_ID:
        await ctx.send("❌ Google Calendar가 설정되지 않았어요. (환경변수 확인)")
        return
    async with ctx.typing():
        event = await parse_event_from_ai(content)
        if not event:
            await ctx.send(
                "❌ 일정 정보를 파악하지 못했어요.\n"
                "날짜와 시간을 포함해서 다시 입력해주세요.\n"
                "예: `/일정추가 2월 25일 오후 2시 회의`"
            )
            return
        start_dt = f"{event['date']}T{event['start_time']}:00+09:00"
        end_dt   = f"{event['date']}T{event['end_time']}:00+09:00"
        ok = await calendar_add_event(
            event["title"], start_dt, end_dt, event.get("description", "")
        )
        if ok:
            await ctx.send(
                f"📅 **캘린더 추가 완료!**\n"
                f"**제목:** {event['title']}\n"
                f"**날짜:** {event['date']}\n"
                f"**시간:** {event['start_time']} ~ {event['end_time']}"
            )
        else:
            await ctx.send(
                "❌ 캘린더 추가 중 오류가 발생했어요.\n"
                "Railway 로그에서 `[Google Calendar 서비스 오류]` 메시지를 확인해주세요.\n"
                "(`GOOGLE_CREDENTIALS_JSON` 형식 오류일 가능성이 높아요)"
            )

@bot.command(name="오늘일정")
async def today_schedule(ctx):
    """오늘 구글 캘린더 일정 조회"""
    if not GOOGLE_CALENDAR_ID:
        await ctx.send("❌ Google Calendar가 설정되지 않았어요.")
        return
    async with ctx.typing():
        today    = date.today()
        time_min = f"{today.isoformat()}T00:00:00+09:00"
        time_max = f"{today.isoformat()}T23:59:59+09:00"
        events   = await calendar_get_events(time_min, time_max)
        if not events:
            await ctx.send(f"📅 오늘 ({today.isoformat()}) 등록된 일정이 없어요!")
            return
        lines = [f"📅 **오늘 ({today.isoformat()}) 일정**\n"]
        for e in events:
            time_str = e["start"][11:16] if "T" in e["start"] else "(종일)"
            lines.append(f"• {time_str} — {e['title']}")
        await ctx.send("\n".join(lines))

@bot.command(name="이번주일정")
async def week_schedule(ctx):
    """이번 주 구글 캘린더 일정 조회"""
    if not GOOGLE_CALENDAR_ID:
        await ctx.send("❌ Google Calendar가 설정되지 않았어요.")
        return
    async with ctx.typing():
        today    = date.today()
        week_end = today + timedelta(days=7)
        time_min = f"{today.isoformat()}T00:00:00+09:00"
        time_max = f"{week_end.isoformat()}T23:59:59+09:00"
        events   = await calendar_get_events(time_min, time_max)
        if not events:
            await ctx.send("📅 이번 주 등록된 일정이 없어요!")
            return
        lines = [f"📅 **이번 주 일정 ({today} ~ {week_end})**\n"]
        for e in events:
            if "T" in e["start"]:
                day      = e["start"][:10]
                time_str = e["start"][11:16]
                lines.append(f"• {day} {time_str} — {e['title']}")
            else:
                lines.append(f"• {e['start']} (종일) — {e['title']}")
        await send_long_message(ctx, "\n".join(lines))

# ─── 할일 커맨드 ──────────────────────────────────────
@bot.command(name="할일추가")
async def add_todo_cmd(ctx, *, content: str = None):
    """Notion 할일 DB에 할일 추가. 예: /할일추가 보고서 작성"""
    if not content:
        await ctx.send("❌ 할일 내용을 입력해주세요.\n예: `/할일추가 보고서 작성`")
        return
    if not NOTION_TODO_DB_ID:
        await ctx.send("❌ Notion 할일 DB가 설정되지 않았어요. (NOTION_TODO_DB_ID 확인)")
        return
    ok = await notion_add_todo(content)
    if ok:
        await ctx.send(f"✅ 할일 추가 완료!\n**{content}**")
    else:
        await ctx.send("❌ 할일 추가 중 오류가 발생했어요.")

@bot.command(name="할일목록")
async def list_todos_cmd(ctx):
    """Notion DB에서 미완료 할일 목록 조회"""
    if not NOTION_TODO_DB_ID:
        await ctx.send("❌ Notion 할일 DB가 설정되지 않았어요.")
        return
    async with ctx.typing():
        todos = await notion_get_todos()
        if not todos:
            await ctx.send("✅ 미완료 할일이 없어요! 모두 완료했나요? 🎉")
            return
        priority_emoji = {"높음": "🔴", "중간": "🟡", "낮음": "🟢"}
        lines = ["**📋 미완료 할일 목록**\n"]
        for i, todo in enumerate(todos, 1):
            emoji    = priority_emoji.get(todo["priority"], "⬜")
            due_str  = f" | 마감: {todo['due']}" if todo["due"] else ""
            lines.append(f"{i}. {emoji} {todo['title']}{due_str}")
        await send_long_message(ctx, "\n".join(lines))

@bot.command(name="할일완료")
async def complete_todo_cmd(ctx, *, title: str = None):
    """Notion 할일 완료 처리. 예: /할일완료 보고서 작성"""
    if not title:
        await ctx.send("❌ 완료할 할일 이름을 입력해주세요.\n예: `/할일완료 보고서 작성`")
        return
    if not NOTION_TODO_DB_ID:
        await ctx.send("❌ Notion 할일 DB가 설정되지 않았어요.")
        return
    ok = await notion_complete_todo(title)
    if ok:
        await ctx.send(f"✅ **{title}** 완료 처리했어요! 수고했어요 🎉")
    else:
        await ctx.send(f"❌ '{title}' 할일을 찾지 못했어요. 이름을 다시 확인해주세요.")

# ─── 수정 커맨드 ──────────────────────────────────────
@bot.command(name="할일수정")
async def update_todo_cmd(ctx, *, content: str = None):
    """Notion 할일 수정. 예: /할일수정 기존이름 | 새이름 또는 /할일수정 이름 | 마감:2026-03-01 또는 /할일수정 이름 | 우선순위:높음"""
    if not content or "|" not in content:
        await ctx.send(
            "❌ 형식을 맞춰주세요.\n"
            "예시:\n"
            "`/할일수정 기존이름 | 새이름`\n"
            "`/할일수정 기존이름 | 마감:2026-03-01`\n"
            "`/할일수정 기존이름 | 우선순위:높음`"
        )
        return
    if not NOTION_TODO_DB_ID:
        await ctx.send("❌ Notion 할일 DB가 설정되지 않았어요.")
        return
    parts     = content.split("|", 1)
    old_title = parts[0].strip()
    change    = parts[1].strip()

    new_title = due_date = priority = ""
    if change.startswith("마감:"):
        due_date = change.replace("마감:", "").strip()
    elif change.startswith("우선순위:"):
        priority = change.replace("우선순위:", "").strip()
    else:
        new_title = change

    ok = await notion_update_todo(old_title, new_title, due_date, priority)
    if ok:
        await ctx.send(f"✅ **{old_title}** 할일을 수정했어요!")
    else:
        await ctx.send(f"❌ '{old_title}' 할일을 찾지 못했어요. 이름을 다시 확인해주세요.")

@bot.command(name="메모수정")
async def update_memo_cmd(ctx, *, content: str = None):
    """Notion 메모 수정. 예: /메모수정 기존제목 | 새 내용"""
    if not content or "|" not in content:
        await ctx.send("❌ 형식을 맞춰주세요.\n예: `/메모수정 기존제목 | 새로운 내용`")
        return
    if not NOTION_MEMO_DB_ID:
        await ctx.send("❌ Notion 메모 DB가 설정되지 않았어요.")
        return
    parts   = content.split("|", 1)
    title   = parts[0].strip()
    new_val = parts[1].strip()

    # 제목 변경인지 내용 변경인지 구분: "제목:" 접두어가 있으면 제목 변경
    if new_val.startswith("제목:"):
        ok = await notion_update_memo(title, new_title=new_val.replace("제목:", "").strip())
    else:
        ok = await notion_update_memo(title, new_content=new_val)
    if ok:
        await ctx.send(f"✅ **{title}** 메모를 수정했어요!")
    else:
        await ctx.send(f"❌ '{title}' 메모를 찾지 못했어요. 제목을 다시 확인해주세요.")

@bot.command(name="헬스수정")
async def update_health_cmd(ctx, *, content: str = None):
    """Notion 헬스 기록 수정. 예: /헬스수정 2026-02-18 | 수정할 내용"""
    if not content or "|" not in content:
        await ctx.send("❌ 형식을 맞춰주세요.\n예: `/헬스수정 2026-02-18 | 수정할 내용`")
        return
    if not NOTION_HEALTH_DB_ID:
        await ctx.send("❌ Notion 헬스 DB가 설정되지 않았어요.")
        return
    parts       = content.split("|", 1)
    date_str    = parts[0].strip()
    new_content = parts[1].strip()
    ok = await notion_update_health_log(date_str, new_content)
    if ok:
        await ctx.send(f"✅ **{date_str}** 헬스 기록을 수정했어요!")
    else:
        await ctx.send(f"❌ '{date_str}' 날짜의 헬스 기록을 찾지 못했어요. 날짜 형식(YYYY-MM-DD)을 확인해주세요.")

# ─── 메모 커맨드 ──────────────────────────────────────
@bot.command(name="메모")
async def save_memo_cmd(ctx, *, content: str = None):
    """Notion 메모 DB에 저장. 예: /메모 제목 | 내용 (| 없으면 전체가 제목)"""
    if not content:
        await ctx.send("❌ 메모 내용을 입력해주세요.\n예: `/메모 아이디어 | 내용을 여기에`")
        return
    if not NOTION_MEMO_DB_ID:
        await ctx.send("❌ Notion 메모 DB가 설정되지 않았어요. (NOTION_MEMO_DB_ID 확인)")
        return
    if "|" in content:
        parts = content.split("|", 1)
        title = parts[0].strip()
        body  = parts[1].strip()
    else:
        title = content[:50]
        body  = content
    ok = await notion_save_memo(title, body)
    if ok:
        await ctx.send(f"📝 메모 저장 완료!\n**{title}**")
    else:
        await ctx.send("❌ 메모 저장 중 오류가 발생했어요.")

# ─── 도움말 ───────────────────────────────────────────
@bot.command(name="도움말")
async def help_command(ctx):
    """봇 전체 사용법 출력"""
    help_text = """**🤖 AI 비서 봇 사용법**

**📺 채널별 자동 모드:**
`#헬스` → 운동 코치 + 식단 어드바이저 💪 (저장 시 Notion 헬스 일지 저장)
`#번역` → 번역 모드 🌏 (번역할 때마다 Notion 자동 저장)
`#일정` → 일정 관리 모드 📅 (일정 언급 시 캘린더 자동 추가)
그 외 채널 → 만능 비서 모드 🤖

**⚙️ 기본 커맨드:**
`/저장` — 오늘 대화 AI 요약 후 파일 & Notion 저장
`/초기화` — 이 채널 대화 히스토리 삭제
`/히스토리` — 현재 저장된 대화 수 확인
`/모드` — 현재 채널 모드 및 사용 모델 확인

**📅 일정 커맨드:**
`/일정추가 [내용]` — AI가 파싱해서 구글 캘린더에 추가
`/오늘일정` — 오늘 구글 캘린더 일정 조회
`/이번주일정` — 이번 주 일정 조회

**✅ 할일 커맨드:**
`/할일추가 [내용]` — Notion 할일 DB에 추가
`/할일목록` — 미완료 할일 목록 조회
`/할일완료 [할일명]` — 해당 할일 완료 처리
`/할일수정 [이름] | [새이름]` — 할일 이름 변경
`/할일수정 [이름] | 마감:2026-03-01` — 마감일 변경
`/할일수정 [이름] | 우선순위:높음` — 우선순위 변경

**📝 메모 커맨드:**
`/메모 [제목] | [내용]` — Notion 메모 DB에 저장
`/메모수정 [제목] | [새 내용]` — 메모 내용 수정

**💪 헬스 커맨드:**
`/헬스수정 2026-02-18 | [수정 내용]` — 특정 날짜 헬스 기록 수정

`/도움말` — 이 메시지"""
    await send_long_message(ctx, help_text)

bot.run(DISCORD_TOKEN)
