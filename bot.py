import os
import json
import time
import hashlib
import logging
import traceback
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

import discord
from discord.ext import tasks, commands
from dotenv import load_dotenv

# -------- 공통 설정 --------
logging.basicConfig(level=logging.INFO)
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# 채널
ALERT_CHANNEL_ID  = int(os.getenv("ALERT_CHANNEL_ID", "0"))    # 결계-필보
LOG_CHANNEL_ID    = int(os.getenv("LOG_CHANNEL_ID", "0"))      # 봇-로그(옵션)
NOTICE_CHANNEL_ID = int(os.getenv("NOTICE_CHANNEL_ID", "0"))   # 공지-알림

# 역할
HOUR_TICK_ROLE_ID   = int(os.getenv("HOUR_TICK_ROLE_ID", "0"))     # 정각 알림
FIELD_BOSS_ROLE_ID  = int(os.getenv("FIELD_BOSS_ROLE_ID", "0"))    # 필드보스
ANNOUNCE_ROLE_ID    = int(os.getenv("ANNOUNCE_ROLE_ID", "0"))      # 공지다 멍!

# 시간대 KST (tzdata 없을 때 폴백)
try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9))

# 디스코드 봇 기본
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
allowed = discord.AllowedMentions(everyone=False, users=False, roles=True)
bot = commands.Bot(command_prefix="!", intents=intents, allowed_mentions=allowed)

def get_channel(cid: int):
    ch = bot.get_channel(cid)
    # TextChannel, News(Announcement)Channel, Thread 모두 메시지 전송 가능
    if isinstance(ch, (discord.TextChannel, discord.Thread)):
        return ch
    # send 메서드가 있으면 써도 됨(안전망)
    return ch if getattr(ch, "send", None) else None

async def report_error(prefix: str, err: Exception):
    tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    msg = f"❗ {prefix}\n```\n{tb[-1800:]}\n```"
    ch = get_channel(LOG_CHANNEL_ID)
    if ch: await ch.send(msg)
    else: print(msg)

# ---------------- 정각/필드보스 알림 ----------------
@tasks.loop(minutes=1)
async def tick_loop():
    try:
        now = datetime.now(KST)
        ch = get_channel(ALERT_CHANNEL_ID)
        if not ch: return

        # 1) 정각 알림 (09:00 ~ 23:00)
        if now.minute == 0 and 9 <= now.hour <= 23:
            role = f"<@&{HOUR_TICK_ROLE_ID}>" if HOUR_TICK_ROLE_ID else ""
            await ch.send(f"{role} ⏰ `{now:%m/%d (%a)}` **{now:%H:%M} 정각 알림!**")

        # 2) 필드보스 알림
        if now.minute == 0 and now.hour in {12, 18, 20, 22}:
            role = f"<@&{FIELD_BOSS_ROLE_ID}>" if FIELD_BOSS_ROLE_ID else ""
            await ch.send(f"{role} 🐲 **필드보스 시간!** `{now:%H:%M}`")

    except Exception as e:
        await report_error("tick_loop 에러", e)

# ---------------- 새 글 자동 알림 ----------------
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (DiscordBot; NPC Guild Helper)"
})

NEWS_SOURCES = {
    # 이름: (URL, 태그용 이모지/라벨)
    "공지사항": ("https://mabinogimobile.nexon.com/News/Notice", "📣 공지"),
    "업데이트": ("https://mabinogimobile.nexon.com/News/Update", "🛠 업데이트"),
    "이벤트":   ("https://mabinogimobile.nexon.com/News/Events?headlineId=2501", "🎉 이벤트"),
    "에린노트": ("https://mabinogimobile.nexon.com/News/Devnote", "📔 에린노트"),
}

STATE_FILE = "seen.json"
_bootstrap_done = False  # 첫 실행에 과거 글 폭탄 방지

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {k: [] for k in NEWS_SOURCES.keys()}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def normalize_link(url: str) -> str:
    # 링크 자체를 해시로 ID화
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return h

def fetch_latest_items(name: str, url: str, limit: int = 5):
    """
    사이트 구조가 바뀌어도 최대한 버틸 수 있게
    - 페이지 내의 <a>들 중 'Notice/Update/Events/Devnote'로 이어지는 것 위주로 추림
    - title/text와 href를 같이 반환
    """
    try:
        r = SESSION.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        anchors = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # 상대경로 보정
            if href.startswith("/"):
                href = "https://mabinogimobile.nexon.com" + href
            # 뉴스 도메인으로 이어지는 링크만 추림
            if "mabinogimobile.nexon.com" in href and any(
                seg in href for seg in ("/News/Notice", "/News/Update", "/News/Events", "/News/Devnote")
            ):
                title = a.get_text(strip=True)
                if title:
                    anchors.append((title, href))

        # 중복 제거, 앞쪽 것 우선
        seen = set()
        items = []
        for title, href in anchors:
            key = normalize_link(href)
            if key in seen: continue
            seen.add(key)
            items.append({"id": key, "title": title, "link": href})
            if len(items) >= limit:
                break
        return items

    except Exception as e:
        logging.warning(f"[news] {name} fetch 실패: {e}")
        return []

async def announce_news_item(ch: discord.TextChannel, label: str, item: dict):
    role = f"<@&{ANNOUNCE_ROLE_ID}>" if ANNOUNCE_ROLE_ID else ""
    title = item["title"]
    link  = item["link"]
    now   = datetime.now(KST).strftime("%m/%d %H:%M")
    msg = f"{role} {label} **새 글**\n🗓 `{now}`\n🔗 {link}\n**{title}**"
    await ch.send(msg)

@tasks.loop(minutes=5)
async def news_loop():
    global _bootstrap_done
    try:
        ch = get_channel(NOTICE_CHANNEL_ID)
        if not ch:
            return

        state = load_state()

        for name, (url, label) in NEWS_SOURCES.items():
            items = fetch_latest_items(name, url, limit=5)
            if not items:
                continue

            known = set(state.get(name, []))
            new_items = [it for it in items if it["id"] not in known]

            # 첫 기동 직후엔 상태만 갱신하고 알림은 생략(폭탄 방지)
            if not _bootstrap_done:
                state[name] = list({*known, *[it["id"] for it in items]})
                save_state(state)
                continue

            # 새 글이면 최신순으로 알림
            for it in reversed(new_items):
                await announce_news_item(ch, label, it)
                known.add(it["id"])
                state[name] = list(known)
                save_state(state)

            # 너무 빠른 연속 요청 방지
            await bot.loop.run_in_executor(None, lambda: time.sleep(0.5))

    except Exception as e:
        await report_error("news_loop 에러", e)

# ===== 알림 역할 토글 패널 (기존 역할 ID 사용) =====
import discord

async def toggle_role(member: discord.Member, role: discord.Role) -> str:
    if role in member.roles:
        await member.remove_roles(role, reason="알림 토글")
        return f"❎ `{role.name}` 해제했어요"
    else:
        await member.add_roles(role, reason="알림 토글")
        return f"✅ `{role.name}` 설정했어요"

class SimpleAlertPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # 영구 버튼 (재부팅 후에도 복원)

    @discord.ui.button(label="결계 알림", style=discord.ButtonStyle.primary, custom_id="alert:hour")
    async def hour_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = interaction.guild.get_role(HOUR_TICK_ROLE_ID)
        if role is None:
            return await interaction.response.send_message("❌ `HOUR_TICK_ROLE_ID` 역할을 못 찾았어요", ephemeral=True)
        msg = await toggle_role(interaction.user, role)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="필보 알림", style=discord.ButtonStyle.success, custom_id="alert:boss")
    async def boss_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = interaction.guild.get_role(FIELD_BOSS_ROLE_ID)
        if role is None:
            return await interaction.response.send_message("❌ `FIELD_BOSS_ROLE_ID` 역할을 못 찾았어요", ephemeral=True)
        msg = await toggle_role(interaction.user, role)
        await interaction.response.send_message(msg, ephemeral=True)

# ---------------- 봇 이벤트/실행 ----------------
@bot.command()
async def 핑(ctx: commands.Context):
    await ctx.send("퐁! ✅ 봇 살아있음")

@bot.command()
async def 테스트공지(ctx):
    ch = get_channel(NOTICE_CHANNEL_ID)
    role = f"<@&{ANNOUNCE_ROLE_ID}>" if ANNOUNCE_ROLE_ID else ""
    now  = datetime.now(KST).strftime("%m/%d %H:%M")
    msg = f"{role} 📣 테스트 새 글 알림!\n🗓 `{now}`\n🔗 https://mabinogimobile.nexon.com/News/Notice\n**테스트용 공지입니다.**"
    if ch:
        await ch.send(msg)
    else:
        await ctx.send("❌ NOTICE_CHANNEL_ID 채널을 찾을 수 없어요")

@bot.event
async def setup_hook():
    # custom_id로 복원되므로 재부팅해도 버튼이 살아 있어요
    bot.add_view(SimpleAlertPanel())

@bot.command()
@commands.has_permissions(manage_guild=True)  # 운영진만 패널 뿌리게
async def 알림패널(ctx: commands.Context):
    embed = discord.Embed(
        title="레이드 & 정각 알리미",
        description="버튼을 눌러 알림을 설정하세요.\n" \
        "버튼을 다시 누르면 알림이 해제됩니다.\n" \
        "필드보스 등장·결계 시간에 알림을 보냅니다.",)
    await ctx.send(embed=embed, view=SimpleAlertPanel())

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")
    if LOG_CHANNEL_ID:
        log_ch = get_channel(LOG_CHANNEL_ID)
        if log_ch:
            await log_ch.send("🤖 봇이 온라인입니다. 스케줄러 시작!")

    if not tick_loop.is_running():
        tick_loop.start()

    # news_loop는 첫 사이클은 부트스트랩(기존 글 상태만 기록)
    global _bootstrap_done
    _bootstrap_done = False
    if not news_loop.is_running():
        news_loop.start()
        # 첫 루프 한 번 지나가고 난 뒤부터 알림 시작
        bot.loop.call_later(10, lambda: globals().__setitem__("_bootstrap_done", True))

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN이 .env에 없습니다!")
    bot.run(TOKEN)
