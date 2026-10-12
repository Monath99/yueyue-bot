import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import random
import datetime
import json
import os
import re
# ==== 以下是从 bot (6).py 搬过来的 ====
import io
import sqlite3
import hashlib
import secrets
import traceback
from collections import defaultdict

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================
#        从 bot (6).py 缝合进来的 附件下载系统
# ============================================================
# ============================================================
#   下载系统权限：按「类别」划分，每个类别允许哪些身份组
# ============================================================
CATEGORY_ADMIN_ROLES = {
    # 社区一 · 卡区类别
    1543924640676581426: {
        1546164752944865462,
        1543964484270559312,
        1543686003443638382,
    },
    # 社区二 · 卡区类别
    1554325545863487508: {
        1559125126815944774,
        1559126102603989064,
        1559125638705848360,
        1558937973188329522,
        1558937307862794240,
    },
}

# 所有允许使用下载系统的类别（自动从上面推导出来）
ALL_DOWNLOAD_CATEGORIES = set(CATEGORY_ADMIN_ROLES.keys())


def get_category_id(channel):
    """从频道/子区往上找所属类别的 ID"""
    cur = channel
    for _ in range(6):
        if cur is None:
            return None
        if isinstance(cur, discord.CategoryChannel):
            return cur.id
        cat_id = getattr(cur, "category_id", None)
        if cat_id:
            return cat_id
        parent = getattr(cur, "parent", None)
        if parent is None:
            return None
        cur = parent
    return None


def in_download_category(channel) -> bool:
    """频道是否属于允许的两个类别之一"""
    return get_category_id(channel) in ALL_DOWNLOAD_CATEGORIES


def is_admin_in(member, channel) -> bool:
    """检查成员是否有权限在「该频道所属类别」使用管理功能"""
    if not isinstance(member, discord.Member):
        return False
    # 服务器服主永远有权限
    if member.guild and member.id == member.guild.owner_id:
        return True
    cat_id = get_category_id(channel)
    if cat_id is None:
        return False
    allowed = CATEGORY_ADMIN_ROLES.get(cat_id)
    if not allowed:
        return False
    return any(role.id in allowed for role in member.roles)

DATA_DIR = "data"
FILES_DIR = os.path.join(DATA_DIR, "files")
DB_PATH = os.path.join(DATA_DIR, "files.db")
DOWNLOAD_EXPIRE_SECONDS = 120


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(FILES_DIR, exist_ok=True)
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS files (
        id            TEXT PRIMARY KEY,
        filename      TEXT NOT NULL,
        filepath      TEXT NOT NULL,
        size          INTEGER,
        uploader_id   INTEGER,
        uploader_name TEXT,
        channel_id    INTEGER,
        pw_hash       TEXT,
        pw_salt       TEXT,
        created_at    TEXT
    );
    CREATE TABLE IF NOT EXISTS downloads (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id       TEXT NOT NULL,
        user_id       INTEGER NOT NULL,
        user_name     TEXT,
        downloaded_at TEXT
    );
    CREATE TABLE IF NOT EXISTS post_messages (
        channel_id    INTEGER PRIMARY KEY,
        message_id    INTEGER,
        created_at    TEXT
    );
    CREATE TABLE IF NOT EXISTS post_user_passwords (
        channel_id INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        pw_hash    TEXT,
        pw_salt    TEXT,
        pw_plain   TEXT,
        created_at TEXT,
        PRIMARY KEY (channel_id, user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_dl_file ON downloads(file_id);
    CREATE INDEX IF NOT EXISTS idx_files_channel ON files(channel_id);
    """)
    conn.commit()

    cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
    for col, typ in [("pw_plain", "TEXT"), ("custom_name", "TEXT"),
                     ("post_seq", "INTEGER"), ("deleted_at", "TEXT")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE files ADD COLUMN {col} {typ}")
    conn.commit()

    old = conn.execute(
        "SELECT id, channel_id FROM files WHERE post_seq IS NULL ORDER BY channel_id, created_at"
    ).fetchall()
    if old:
        grouped = defaultdict(list)
        for row in old:
            grouped[row["channel_id"]].append(row["id"])
        for channel_id, ids in grouped.items():
            mx = conn.execute(
                "SELECT MAX(post_seq) AS m FROM files WHERE channel_id=?",
                (channel_id,)
            ).fetchone()
            start = (mx["m"] or 0) + 1
            for i, fid in enumerate(ids):
                conn.execute("UPDATE files SET post_seq=? WHERE id=?", (start + i, fid))
        conn.commit()
    conn.close()


def hash_password(password: str, salt: str = None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return h, salt

def fmt_time(iso_str):
    if not iso_str:
        return ""
    return iso_str[:16].replace("T", " ")


def parse_seq(s):
    if s is None:
        return None
    s = s.strip().lstrip("#").strip()
    if not s.isdigit():
        return None
    n = int(s)
    return n if n > 0 else None


def get_user_password(conn, channel_id, user_id):
    return conn.execute(
        "SELECT pw_hash, pw_salt, pw_plain FROM post_user_passwords WHERE channel_id=? AND user_id=?",
        (channel_id, user_id)
    ).fetchone()


def save_user_password(conn, channel_id, user_id, pw_hash, pw_salt, pw_plain):
    conn.execute(
        """INSERT OR REPLACE INTO post_user_passwords
           (channel_id, user_id, pw_hash, pw_salt, pw_plain, created_at)
           VALUES (?,?,?,?,?,?)""",
        (channel_id, user_id, pw_hash, pw_salt, pw_plain, datetime.datetime.now().isoformat())
    )
    conn.commit()


async def reply(interaction, content=None, **kwargs):
    if interaction.response.is_done():
        return await interaction.followup.send(content, **kwargs)
    return await interaction.response.send_message(content, **kwargs)


def build_summary_embed(conn, channel_id, owner_id=None):
    rows = conn.execute(
        "SELECT pw_hash FROM files WHERE channel_id=? AND deleted_at IS NULL",
        (channel_id,)
    ).fetchall()
    total = len(rows)

    embed = discord.Embed(
        title="🔒 受保护的附件",
        description="受保护的附件",
        color=0x2b2d31
    )

    if total == 0:
        embed.add_field(name="📎 附件数量", value="暂无附件", inline=True)
        embed.add_field(name="是否有密码：", value="—", inline=True)
    else:
        pw_count = sum(1 for r in rows if r["pw_hash"])
        if pw_count == 0:
            pw_text = "🔓 否"
        elif pw_count == total:
            pw_text = "🔒 是"
        else:
            pw_text = f"🔒 部分（{pw_count}/{total} 有密码）"
        embed.add_field(name="📎 附件数量", value=f"{total} 个文件", inline=True)
        embed.add_field(name="是否有密码：", value=pw_text, inline=True)

    if owner_id:
        embed.set_footer(text=f"帖子创建者：{owner_id}")
    return embed, total


async def update_post_message(conn, channel, channel_id, owner_id=None):
    embed, total = build_summary_embed(conn, channel_id, owner_id)
    view = GetFileView(channel_id)

    post = conn.execute(
        "SELECT * FROM post_messages WHERE channel_id=?", (channel_id,)
    ).fetchone()

    if post is None:
        msg = await channel.send(embed=embed, view=view)
        conn.execute(
            "INSERT INTO post_messages (channel_id, message_id, created_at) VALUES (?,?,?)",
            (channel_id, msg.id, datetime.datetime.now().isoformat())
        )
        conn.commit()
        return total
    else:
        try:
            msg = await channel.fetch_message(post["message_id"])
            await msg.edit(embed=embed, view=view)
            return total
        except discord.NotFound:
            msg = await channel.send(embed=embed, view=view)
            conn.execute(
                "UPDATE post_messages SET message_id=? WHERE channel_id=?",
                (msg.id, channel_id)
            )
            conn.commit()
            return total


async def _expire_file_message(msg, delay: int = DOWNLOAD_EXPIRE_SECONDS):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    try:
        await msg.edit(
            content=(
                f"⌛ **下载时限已到（{delay} 秒）**，附件已自动失效。\n"
                f"如需重新下载，请重新点击 **📥 获取附件**。"
            ),
            attachments=[]
        )
    except Exception:
        pass


async def deliver_file(interaction, file_id: str, password: str = None):
    conn = db()
    row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()

    if row is None:
        conn.close()
        await reply(interaction, "❌ 文件不存在。", ephemeral=True)
        return

    if row["deleted_at"]:
        conn.close()
        await reply(interaction, "🗑️ 该附件已被删除。", ephemeral=True)
        return

    if row["pw_hash"]:
        if password is None:
            conn.close()
            await reply(interaction, "🔒 该附件需要密码。", ephemeral=True)
            return
        check, _ = hash_password(password.strip(), row["pw_salt"])
        if check != row["pw_hash"]:
            conn.close()
            await reply(interaction, "❌ 密码错误。", ephemeral=True)
            return

    if not os.path.exists(row["filepath"]):
        conn.close()
        await reply(interaction, "❌ 文件已丢失，请联系管理员。", ephemeral=True)
        return

    conn.execute(
        "INSERT INTO downloads (file_id, user_id, user_name, downloaded_at) VALUES (?,?,?,?)",
        (row["id"], interaction.user.id, str(interaction.user), datetime.datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

    display = row["custom_name"] or row["filename"]
    seq = row["post_seq"] or 0
    file = discord.File(row["filepath"], filename=row["filename"])

    msg = await reply(
        interaction,
        content=(
            f"📁 **#{seq:02d} {display}**\n"
            f"⏳ 下载时限：**{DOWNLOAD_EXPIRE_SECONDS} 秒**\n"
            f"请尽快保存到本地，超时后本条消息的附件会自动收回。"
        ),
        file=file,
        ephemeral=True
    )

    if msg is not None:
        asyncio.create_task(_expire_file_message(msg))


# ---------------- 密码弹窗 ----------------
class PasswordModal(discord.ui.Modal, title="🔒 输入下载密码"):
    password = discord.ui.TextInput(
        label="密码",
        placeholder="请输入该附件的下载密码",
        required=True,
        max_length=200
    )

    def __init__(self, file_id: str):
        super().__init__()
        self.file_id = file_id

    async def on_submit(self, interaction):
        try:
            await deliver_file(interaction, self.file_id, self.password.value)
        except Exception:
            traceback.print_exc()


# ---------------- 公共上传执行函数 ----------------
async def _finish_upload(interaction, attachment, custom_name,
                          pw_hash, pw_salt, pw_plain,
                          channel_id, owner_id):
    try:
        file = attachment
        display = custom_name or file.filename

        file_id = secrets.token_hex(4)
        ext = os.path.splitext(file.filename)[1] or ""
        save_path = os.path.join(FILES_DIR, f"{file_id}{ext}")

        try:
            await file.save(save_path)
        except Exception as e:
            await interaction.followup.send(f"❌ 保存文件失败：{e}", ephemeral=True)
            return

        conn = db()
        mx = conn.execute(
            "SELECT MAX(post_seq) AS m FROM files WHERE channel_id=?",
            (channel_id,)
        ).fetchone()
        next_seq = (mx["m"] or 0) + 1

        conn.execute(
            """INSERT INTO files
               (id, filename, filepath, size, uploader_id, uploader_name,
                channel_id, pw_hash, pw_salt, created_at, pw_plain, custom_name, post_seq)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (file_id, file.filename, save_path, file.size or 0,
             interaction.user.id, str(interaction.user), channel_id,
             pw_hash, pw_salt, datetime.datetime.now().isoformat(),
             pw_plain, custom_name, next_seq)
        )
        conn.commit()

        try:
            total = await update_post_message(
                conn, interaction.channel, channel_id,
                owner_id or interaction.user.id
            )
        except discord.Forbidden:
            conn.close()
            await interaction.followup.send(
                "❌ 我没有权限在该帖子发送/编辑消息。请给 Bot 勾选：\n"
                "`查看频道`、`发送消息`、`在子区中发送消息`、`嵌入链接`、`附加文件`、`管理消息`。",
                ephemeral=True
            )
            return

        conn.close()

        if pw_plain:
            extra = f"\n🔑 本次密码：`{pw_plain}`\n（**此消息仅你可见**，下次上传会自动沿用此密码）"
        else:
            extra = "\n🔓 本次未设置密码，任何人可直接下载。"

        await interaction.followup.send(
            f"✅ 已上传 **#{next_seq:02d} {display}**（共 **{total}** 个附件）{extra}",
            ephemeral=True
        )

    except discord.errors.HTTPException as e:
        try:
            await interaction.followup.send(
                f"❌ 发送失败（HTTP {e.status}）：{e.text or e}",
                ephemeral=True
            )
        except Exception:
            traceback.print_exc()
    except Exception as e:
        traceback.print_exc()
        try:
            await interaction.followup.send(
                f"❌ 上传失败：{type(e).__name__}: {e}",
                ephemeral=True
            )
        except Exception:
            traceback.print_exc()


class UploadModalWithPassword(discord.ui.Modal, title="📎 上传附件设置（首次）"):
    custom_name = discord.ui.TextInput(
        label="文件名",
        placeholder="留空 = 直接用原文件名",
        required=False,
        max_length=100
    )
    password = discord.ui.TextInput(
        label="下载密码",
        placeholder="留空 = 无需密码，任何人可直接下载",
        required=False,
        max_length=200
    )

    def __init__(self, attachment, channel_id, owner_id):
        super().__init__()
        self.attachment = attachment
        self.channel_id = channel_id
        self.owner_id = owner_id

    async def on_submit(self, interaction):
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except Exception:
            traceback.print_exc()
            return

        custom_name = self.custom_name.value.strip() if self.custom_name.value else None
        password = self.password.value.strip() if self.password.value else None

        pw_hash = pw_salt = pw_plain = None
        if password:
            pw_plain = password
            pw_hash, pw_salt = hash_password(password)
            conn = db()
            save_user_password(conn, self.channel_id, interaction.user.id,
                               pw_hash, pw_salt, pw_plain)
            conn.close()

        await _finish_upload(
            interaction, self.attachment, custom_name,
            pw_hash, pw_salt, pw_plain,
            self.channel_id, self.owner_id
        )


class UploadModalNoPassword(discord.ui.Modal, title="📎 上传附件设置"):
    custom_name = discord.ui.TextInput(
        label="文件名",
        placeholder="留空 = 直接用原文件名",
        required=False,
        max_length=100
    )

    def __init__(self, attachment, channel_id, owner_id, saved_pw):
        super().__init__()
        self.attachment = attachment
        self.channel_id = channel_id
        self.owner_id = owner_id
        self.saved_pw = saved_pw

    async def on_submit(self, interaction):
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except Exception:
            traceback.print_exc()
            return

        custom_name = self.custom_name.value.strip() if self.custom_name.value else None

        pw_hash = self.saved_pw["pw_hash"]
        pw_salt = self.saved_pw["pw_salt"]
        pw_plain = self.saved_pw["pw_plain"]

        await _finish_upload(
            interaction, self.attachment, custom_name,
            pw_hash, pw_salt, pw_plain,
            self.channel_id, self.owner_id
        )


class FileSelect(discord.ui.Select):
    def __init__(self, files):
        options = []
        for f in files[:25]:
            display = f["custom_name"] or f["filename"]
            date_str = fmt_time(f["created_at"])
            lock = "🔒" if f["pw_hash"] else "🔓"
            seq = f["post_seq"] or 0
            options.append(discord.SelectOption(
                label=f"#{seq:02d} {display}"[:100],
                value=f["id"],
                description=f"📅 {date_str} | {f['size']/1024:.1f} KB | {lock}"[:100]
            ))
        super().__init__(placeholder="请选择要下载的版本（最新在最上）", options=options)

    async def callback(self, interaction):
        file_id = self.values[0]
        try:
            conn = db()
            row = conn.execute("SELECT pw_hash FROM files WHERE id=?", (file_id,)).fetchone()
            conn.close()
            if row is None:
                await interaction.response.send_message("❌ 文件不存在。", ephemeral=True)
                return
            if row["pw_hash"]:
                await interaction.response.send_modal(PasswordModal(file_id))
            else:
                await deliver_file(interaction, file_id)
        except Exception:
            traceback.print_exc()


class FileSelectView(discord.ui.View):
    def __init__(self, files):
        super().__init__(timeout=300)
        self.add_item(FileSelect(files))


class GetFileView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        btn = discord.ui.Button(
            label="获取附件",
            style=discord.ButtonStyle.primary,
            emoji="📥",
            custom_id=f"getfile:{channel_id}"
        )
        btn.callback = self._handle
        self.add_item(btn)

    async def _handle(self, interaction):
        try:
            conn = db()
            files = conn.execute(
                "SELECT * FROM files WHERE channel_id=? AND deleted_at IS NULL ORDER BY post_seq DESC",
                (self.channel_id,)
            ).fetchall()
            conn.close()

            if not files:
                await interaction.response.send_message("📭 该帖子目前没有可下载的附件。", ephemeral=True)
                return

            if len(files) == 1:
                f = files[0]
                if f["pw_hash"]:
                    await interaction.response.send_modal(PasswordModal(f["id"]))
                else:
                    await deliver_file(interaction, f["id"])
            else:
                await interaction.response.send_message(
                    "📥 请选择要下载的版本：",
                    view=FileSelectView(files),
                    ephemeral=True
                )
        except Exception:
            traceback.print_exc()
            try:
                await reply(interaction, "❌ 发生错误，请稍后再试。", ephemeral=True)
            except Exception:
                pass


class TraceExportView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=600)
        self.channel_id = channel_id

    @discord.ui.button(label="保存下载记录", style=discord.ButtonStyle.success, emoji="💾")
    async def save_btn(self, interaction, button):
        try:
            await interaction.response.defer(ephemeral=True)
            conn = db()
            files = conn.execute(
                "SELECT * FROM files WHERE channel_id=? ORDER BY post_seq ASC",
                (self.channel_id,)
            ).fetchall()

            lines = []
            lines.append("=" * 70)
            lines.append("  下载记录导出")
            lines.append("=" * 70)
            lines.append(f"帖子 / 子区 ID ：{self.channel_id}")
            lines.append(f"导出时间      ：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"导出人        ：{interaction.user} ({interaction.user.id})")
            lines.append("=" * 70)
            lines.append("")

            total_downloads = 0
            for f in files:
                display = f["custom_name"] or f["filename"]
                seq = f["post_seq"] or 0
                lines.append(f"【#{seq:02d}】{display}")
                lines.append(f"  文件 ID     ：{f['id']}")
                lines.append(f"  原始文件名  ：{f['filename']}")
                lines.append(f"  文件大小    ：{f['size']/1024:.1f} KB")
                lines.append(f"  上传者      ：{f['uploader_name']} ({f['uploader_id']})")
                lines.append(f"  上传时间    ：{fmt_time(f['created_at'])}")
                if f["deleted_at"]:
                    lines.append(f"  删除时间    ：{fmt_time(f['deleted_at'])}（文件已删，记录保留）")
                else:
                    lines.append(f"  删除时间    ：未删除")
                if f["pw_hash"]:
                    pw_show = f["pw_plain"] if f["pw_plain"] else "（已加密，无明文）"
                    lines.append(f"  是否有密码  ：🔒 是，密码：{pw_show}")
                else:
                    lines.append(f"  是否有密码  ：🔓 否")

                downloads = conn.execute(
                    "SELECT * FROM downloads WHERE file_id=? ORDER BY downloaded_at ASC",
                    (f["id"],)
                ).fetchall()

                if downloads:
                    total_downloads += len(downloads)
                    lines.append(f"  下载记录    ：共 {len(downloads)} 次")
                    for d in downloads:
                        lines.append(
                            f"      - {d['user_name']} ({d['user_id']})  @ {fmt_time(d['downloaded_at'])}"
                        )
                else:
                    lines.append(f"  下载记录    ：无")
                lines.append("")

            lines.append("=" * 70)
            lines.append(f"汇总：{len(files)} 个附件，共 {total_downloads} 次下载")
            lines.append("=" * 70)
            conn.close()

            content = "\n".join(lines)
            fname = f"下载记录_{self.channel_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            file = discord.File(io.BytesIO(content.encode("utf-8")), filename=fname)

            await interaction.followup.send(
                "💾 已导出当前帖子的完整下载记录：",
                file=file,
                ephemeral=True
            )
        except Exception:
            traceback.print_exc()
            try:
                await interaction.followup.send("❌ 导出失败，请看终端报错。", ephemeral=True)
            except Exception:
                pass


# ==================== /上传附件 ====================
@bot.tree.command(name="上传附件", description="上传附件到当前帖子（仅帖子创建者）")
@app_commands.describe(file="要上传的文件")
async def upload_attachment(interaction, file: discord.Attachment):
    try:
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message(
                "❌ 该命令只能在论坛帖子 / 子区线程中使用。", ephemeral=True)
            return
        owner_id = getattr(channel, "owner_id", None)
        if owner_id and owner_id != interaction.user.id:
            await interaction.response.send_message(
                "❌ 只有该帖子的创建者才能上传附件。", ephemeral=True)
            return

        conn = db()
        saved_pw = get_user_password(conn, interaction.channel_id, interaction.user.id)
        conn.close()

        if saved_pw and saved_pw["pw_hash"]:
            modal = UploadModalNoPassword(file, interaction.channel_id, owner_id or 0, saved_pw)
        else:
            modal = UploadModalWithPassword(file, interaction.channel_id, owner_id or 0)

        await interaction.response.send_modal(modal)
    except Exception as e:
        traceback.print_exc()
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ 打开上传设置失败：{e}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ 打开上传设置失败：{e}", ephemeral=True)
        except Exception:
            pass

# ============================================================
#              附件管理面板（/管理）
# ============================================================
def build_trace_embeds(conn, channel_id):
    """生成溯源报告 embed 列表（可能有多页）"""
    files = conn.execute(
        "SELECT * FROM files WHERE channel_id=? ORDER BY post_seq ASC",
        (channel_id,)
    ).fetchall()

    if not files:
        return []

    embeds = []
    current_embed = discord.Embed(title="🔍 溯源报告", color=0x5865F2)
    field_count = 0

    for f in files:
        downloads = conn.execute(
            "SELECT * FROM downloads WHERE file_id=? ORDER BY downloaded_at DESC",
            (f["id"],)
        ).fetchall()

        counter = {}
        for d in downloads:
            uid = d["user_id"]
            if uid not in counter:
                counter[uid] = {"count": 0, "last": d["downloaded_at"]}
            counter[uid]["count"] += 1
            if d["downloaded_at"] > counter[uid]["last"]:
                counter[uid]["last"] = d["downloaded_at"]

        display = f["custom_name"] or f["filename"]
        seq = f["post_seq"] or 0
        title = f"#{seq:02d}  {display}"
        if f["deleted_at"]:
            title += "  🗑️ 已删除"

        lines = []
        lines.append(f"📤 上传：{fmt_time(f['created_at'])}")
        if f["deleted_at"]:
            lines.append(f"🗑️ 删除：{fmt_time(f['deleted_at'])}")
        else:
            lines.append("🗑️ 未删除")

        if f["pw_hash"] and f["pw_plain"]:
            lines.append(f"🔑 密码：`{f['pw_plain']}`")
        elif f["pw_hash"]:
            lines.append("🔑 有密码（加密，无明文）")
        else:
            lines.append("🔓 无密码")

        if counter:
            downloaders = "、".join(
                f"<@{uid}>（{d['count']}次）"
                for uid, d in sorted(counter.items(), key=lambda x: -x[1]["count"])
            )
            lines.append(f"📥 下载者：{downloaders}")
        else:
            lines.append("📥 下载者：无")

        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1000] + "\n...（已截断）"

        current_embed.add_field(name=title[:250], value=value, inline=False)
        field_count += 1

        if field_count >= 20:
            embeds.append(current_embed)
            current_embed = discord.Embed(title="🔍 溯源报告（续）", color=0x5865F2)
            field_count = 0

    if field_count > 0:
        embeds.append(current_embed)

    return embeds


# ---------------- 弹窗：删除附件 ----------------
class DeleteFileModal(discord.ui.Modal, title="🗑️ 删除附件"):
    number = discord.ui.TextInput(
        label="附件编号",
        placeholder="例如 1 或 #1",
        required=True,
        max_length=10
    )
    confirm = discord.ui.TextInput(
        label="确认",
        placeholder="输入 DELETE 确认删除",
        required=True,
        max_length=20
    )

    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction):
        if not is_admin_in(interaction.user, interaction.channel):
            await interaction.response.send_message("❌ 没有权限。", ephemeral=True)
            return
        if self.confirm.value.strip().upper() != "DELETE":
            await interaction.response.send_message(
                "❌ 确认文字不对，已取消。", ephemeral=True)
            return

        seq = parse_seq(self.number.value)
        if seq is None:
            await interaction.response.send_message(
                "❌ 编号格式不对，例如 `1` 或 `#1`。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        conn = db()
        row = conn.execute(
            "SELECT * FROM files WHERE channel_id=? AND post_seq=?",
            (self.channel_id, seq)
        ).fetchone()

        if row is None:
            conn.close()
            await interaction.followup.send(
                f"❌ 找不到编号 `#{seq:02d}` 的附件。", ephemeral=True)
            return
        if row["deleted_at"]:
            conn.close()
            await interaction.followup.send(
                f"⚠️ `#{seq:02d}` 之前已经删除过了。", ephemeral=True)
            return

        try:
            if os.path.exists(row["filepath"]):
                os.remove(row["filepath"])
        except Exception:
            pass

        now = datetime.datetime.now().isoformat()
        conn.execute("UPDATE files SET deleted_at=? WHERE id=?", (now, row["id"]))
        conn.commit()

        try:
            await update_post_message(conn, interaction.channel, self.channel_id)
        except Exception:
            traceback.print_exc()
        conn.close()

        await interaction.followup.send(
            f"🗑️ 已删除 `#{seq:02d} {row['custom_name'] or row['filename']}`\n"
            f"（下载记录保留，可通过「溯源」查看）",
            ephemeral=True
        )


# ---------------- 弹窗：修改 / 取消密码 ----------------
class ChangePasswordModal(discord.ui.Modal, title="🔐 修改附件密码"):
    number = discord.ui.TextInput(
        label="附件编号",
        placeholder="例如 1 或 #1",
        required=True,
        max_length=10
    )
    new_pw = discord.ui.TextInput(
        label="新密码",
        placeholder="留空 = 取消密码（任何人可下载）",
        required=False,
        max_length=200
    )
    confirm = discord.ui.TextInput(
        label="确认",
        placeholder="输入 CHANGE 确认修改",
        required=True,
        max_length=20
    )

    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction):
        if not is_admin_in(interaction.user, interaction.channel):
            await interaction.response.send_message("❌ 没有权限。", ephemeral=True)
            return
        if self.confirm.value.strip().upper() != "CHANGE":
            await interaction.response.send_message(
                "❌ 确认文字不对，已取消。", ephemeral=True)
            return

        seq = parse_seq(self.number.value)
        if seq is None:
            await interaction.response.send_message(
                "❌ 编号格式不对，例如 `1` 或 `#1`。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        conn = db()
        row = conn.execute(
            "SELECT * FROM files WHERE channel_id=? AND post_seq=?",
            (self.channel_id, seq)
        ).fetchone()

        if row is None:
            conn.close()
            await interaction.followup.send(
                f"❌ 找不到编号 `#{seq:02d}` 的附件。", ephemeral=True)
            return
        if row["deleted_at"]:
            conn.close()
            await interaction.followup.send(
                f"⚠️ `#{seq:02d}` 已被删除，不能改密码。", ephemeral=True)
            return

        new_pw = self.new_pw.value.strip() if self.new_pw.value else ""

        if new_pw:
            pw_hash, pw_salt = hash_password(new_pw)
            conn.execute(
                "UPDATE files SET pw_hash=?, pw_salt=?, pw_plain=? WHERE id=?",
                (pw_hash, pw_salt, new_pw, row["id"])
            )
            msg = f"🔐 已修改 `#{seq:02d}` 的密码为：`{new_pw}`"
        else:
            conn.execute(
                "UPDATE files SET pw_hash=NULL, pw_salt=NULL, pw_plain=NULL WHERE id=?",
                (row["id"],)
            )
            msg = f"🔓 已取消 `#{seq:02d}` 的密码，现在任何人都能直接下载。"

        conn.commit()
        conn.close()
        await interaction.followup.send(msg, ephemeral=True)


# ---------------- 弹窗：清除下载记录 ----------------
class ClearDownloadsModal(discord.ui.Modal, title="🧹 清除下载记录"):
    confirm = discord.ui.TextInput(
        label="确认",
        placeholder="输入 CLEAR 确认清除",
        required=True,
        max_length=20
    )

    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction):
        if not is_admin_in(interaction.user, interaction.channel):
            await interaction.response.send_message("❌ 没有权限。", ephemeral=True)
            return
        if self.confirm.value.strip().upper() != "CLEAR":
            await interaction.response.send_message(
                "❌ 确认文字不对，已取消。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        conn = db()
        files = conn.execute(
            "SELECT id FROM files WHERE channel_id=?", (self.channel_id,)
        ).fetchall()

        if not files:
            conn.close()
            await interaction.followup.send(
                "📭 当前帖子没有任何附件。", ephemeral=True)
            return

        total_deleted = 0
        for f in files:
            cur = conn.execute("DELETE FROM downloads WHERE file_id=?", (f["id"],))
            total_deleted += cur.rowcount
        conn.commit()
        conn.close()

        await interaction.followup.send(
            f"🗑️ 已清除当前帖子的 **{total_deleted}** 条下载记录。\n"
            f"⚠️ 附件本身**未删除**，只清了下载记录。\n"
            f"💡 清除后「溯源」会显示「下载者：无」。",
            ephemeral=True
        )


# ---------------- /管理 面板 ----------------
class ManagePanelView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=900)
        self.channel_id = channel_id

    async def _check_admin(self, interaction) -> bool:
        if not is_admin_in(interaction.user, interaction.channel):
            await interaction.response.send_message(
                "❌ 只有该类别对应的身份组或服主可以使用。", ephemeral=True)
            return False
        return True

    # ---- 溯源 ----
    @discord.ui.button(label="溯源", emoji="🔍",
                       style=discord.ButtonStyle.primary, row=0)
    async def trace_btn(self, interaction, button):
        if not await self._check_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        conn = db()
        try:
            files = conn.execute(
                "SELECT id FROM files WHERE channel_id=?",
                (self.channel_id,)
            ).fetchall()
            if not files:
                await interaction.followup.send(
                    "📭 当前帖子没有任何上传过的附件。", ephemeral=True)
                return
            embeds = build_trace_embeds(conn, self.channel_id)
        finally:
            conn.close()

        if not embeds:
            await interaction.followup.send("📭 暂无记录。", ephemeral=True)
            return

        view = TraceExportView(self.channel_id)
        for i, emb in enumerate(embeds):
            if i == len(embeds) - 1:
                await interaction.followup.send(
                    embeds=[emb], view=view, ephemeral=True,
                    content="💡 需要保存完整下载记录？点下方按钮 👇")
            else:
                await interaction.followup.send(embeds=[emb], ephemeral=True)

    # ---- 查看密码 ----
    @discord.ui.button(label="查看密码", emoji="🔑",
                       style=discord.ButtonStyle.secondary, row=0)
    async def pw_btn(self, interaction, button):
        if not await self._check_admin(interaction):
            return

        conn = db()
        rows = conn.execute(
            "SELECT post_seq, custom_name, filename, pw_hash, pw_plain "
            "FROM files WHERE channel_id=? AND deleted_at IS NULL "
            "ORDER BY post_seq ASC",
            (self.channel_id,)
        ).fetchall()
        conn.close()

        if not rows:
            await interaction.response.send_message(
                "📭 当前帖子没有有效附件。", ephemeral=True)
            return

        lines = []
        for r in rows:
            name = r["custom_name"] or r["filename"]
            seq = r["post_seq"] or 0
            if r["pw_hash"]:
                pw = r["pw_plain"] or "（无明文，已加密）"
                lines.append(f"`#{seq:02d}` {name} → 🔒 `{pw}`")
            else:
                lines.append(f"`#{seq:02d}` {name} → 🔓 无密码")

        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n...（已截断）"

        await interaction.response.send_message(
            f"🔑 当前帖子的附件密码：\n{text}", ephemeral=True)

    # ---- 清除下载记录 ----
    @discord.ui.button(label="清除下载记录", emoji="🧹",
                       style=discord.ButtonStyle.danger, row=0)
    async def clear_btn(self, interaction, button):
        if not await self._check_admin(interaction):
            return
        await interaction.response.send_modal(ClearDownloadsModal(self.channel_id))

    # ---- 删除附件 ----
    @discord.ui.button(label="删除附件", emoji="🗑️",
                       style=discord.ButtonStyle.danger, row=1)
    async def del_btn(self, interaction, button):
        if not await self._check_admin(interaction):
            return
        await interaction.response.send_modal(DeleteFileModal(self.channel_id))

    # ---- 修改密码 ----
    @discord.ui.button(label="修改密码", emoji="🔐",
                       style=discord.ButtonStyle.success, row=1)
    async def pw_edit_btn(self, interaction, button):
        if not await self._check_admin(interaction):
            return
        await interaction.response.send_modal(ChangePasswordModal(self.channel_id))


@bot.tree.command(name="管理", description="【管理员/服主】附件管理面板")
async def manage(interaction: discord.Interaction):
    if not is_admin_in(interaction.user, interaction.channel):
        await interaction.response.send_message(
            "❌ 只有该类别对应的身份组或服主可以使用。", ephemeral=True)
        return

    conn = db()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM files WHERE channel_id=? AND deleted_at IS NULL",
        (interaction.channel_id,)
    ).fetchone()
    conn.close()
    total = row["c"] if row else 0

    embed = discord.Embed(
        title="🛠️ 附件管理面板",
        description=(
            f"当前帖子：<#{interaction.channel_id}>\n"
            f"有效附件：**{total}** 个\n\n"
            f"👇 点下面的按钮进行操作\n"
            f"· 🔍 **溯源**：查看每个附件的下载者\n"
            f"· 🔑 **查看密码**：列出所有附件密码\n"
            f"· 🧹 **清除下载记录**：只清记录，不删附件\n"
            f"· 🗑️ **删除附件**：弹窗输入编号 + 确认词 `DELETE`\n"
            f"· 🔐 **修改密码**：弹窗输入编号 + 新密码（留空 = 取消密码）+ 确认词 `CHANGE`"
        ),
        color=0x5865F2
    )
    await interaction.response.send_message(
        embed=embed, view=ManagePanelView(interaction.channel_id), ephemeral=True)


# ==============================================================
#                  五子棋 / 骗子酒馆 · 全局配置
# ==============================================================
ALL_COLS = "ABCDEFGHIJKLMNO"
GOMOKU_TIMEOUT = 30
_games = {}

# 只有在这个频道里 @月月 才会触发自动回复（斜杠指令不受影响）
MENTION_CHANNEL_ID = 1543925433752358982

# 「预设指路」自动回复
PRESET_LINK = "https://discord.com/channels/1543680882081013830/1558820934691979305"
PRESET_KEYWORDS = ["推荐预设", "破限好的预设", "kemini", "kemini缝合"]

# 服务器ID : 骗子酒馆频道ID
LIAR_CHANNEL_CONFIG = {
    1543680882081013830: 1557161825521631322,  # 第一服务器 -> 第一服务器频道
    1554304629335130133: 1559119717342515240,  # 第二服务器 -> 第二服务器频道
}

# 保留这个集合，用于后续监听器判断规则使用（自动生成，不要手动改）
LIAR_CHANNEL_IDS = set(LIAR_CHANNEL_CONFIG.values())

LIAR_MAX_PLAYERS = 4
LIAR_TIMEOUT = 30
LIAR_CHALLENGE_TIMEOUT = 20
_liar_games = {}

# --- 月月自身数据（原封不动） ---
bot_data = {
    "hunger": 50, "thirst": 50, "energy": 50, "affection": 0,
    "thoughts": [
        "想吃小鱼干", "想吃三色丸子", "想吃鲷鱼烧", "想吃金枪鱼罐头",
        "想吃猫薄荷饼干", "想吃章鱼烧", "想吃奶油泡芙", "想吃烤秋刀鱼",
        "想吃草莓大福", "想吃鲜虾寿司", "想吃鱼骨饼干", "想吃小虾干",
        "想吃糙米鱼饼", "想吃烤鱼串", "想被月么呐摸摸头", "想给萌新指路", 
        "想睡觉", "想玩触手打结"
    ]
}

pat_cooldown = {}
feed_cooldown = {}
milk_tea_waiting = {}

# ================= @月月 的梗彩蛋 =================
# 想加梗就往这个表里塞：(关键词, (回复1, 回复2, ...))
MEME_EGGS = [
    (("v我50", "v50", "疯狂星期四", "kfc"), (
        "本座堂堂触手霸主，居然沦落到给你凑五十块炸鸡钱？行吧本座掏了，但鸡腿归本座。🍗",
        "今天星期四，本座的钱包比脸还干净。💸",
    )),
    (("退退退", "退！退！退", "退！退！退！"), (
        "退！退！退！本座的触手比你长！🐙",
        "本座也退，退到小鱼干堆里不出来。🐟",
    )),
    (("泰裤辣", "太酷啦"), (
        "泰裤辣！就是泰裤辣！本座的触手也泰裤辣！✨",
        "泰裤辣到本座八条触手当场打了个中国结。💦",
    )),
    (("遥遥领先",), (
        "遥遥领先！本座摸鱼的速度遥遥领先全服桌宠。🏆",
        "论吃小鱼干，本座遥遥领先，第二名连尾灯都看不到。🐟",
    )),
    (("摆烂", "开摆", "躺平"), (
        "摆烂是吧？本座陪你。触手一摊，谁也不许努力。🛌",
        "本座早就躺好了，你怎么才来。🛌",
    )),
    (("唱首歌", "唱歌", "唱一个"), (
        "本座是条小章鱼，八条腿都很调皮♪ …好听吗？好听就是好歌。🎤",
        "本座唱歌不看音准，看心情。🎤",
    )),
    (("跳个舞", "跳舞", "舞一个"), (
        "本座给你跳一个！触手打结舞！…哎呀，缠住自己了。💦",
        "节目名：《八爪乱舞》，表演者：本座。🐙",
    )),
    (("晚安", "睡了", "睡觉了", "去睡了"), (
        "晚安！本座把触手都收好，就睡在你枕头边上。🌙",
        "快去睡，本座守着你的梦。谁在梦里欺负你，本座咬他。🦊",
    )),
    (("早安", "早上好"), (
        "早上好！本座已经醒了八分钟了，一直在等你叫本座。🌞",
        "早！本座把小鱼干给你留着呢。🐟",
    )),
    (("生日快乐",), (
        "生日快乐！本座给你唱生日歌，跑调的那种，但心意是真的。🎂",
        "生日快乐！许愿的时候记得把本座也算进去。🎂",
    )),
    (("emo了", "好emo", "难过", "不开心", "心情不好"), (
        "呜…谁惹你了？本座用八条触手把他缠起来。先抱一下。🫂",
        "本座把耳朵借给你摸，摸到你不难受为止。🦊",
    )),
    (("不想上班", "不想打工", "想辞职", "上班好累"), (
        "懂！本座也不想上班。给，小鱼干一根，吃完了再当牛马。🐟",
        "上班？本座只会上桌。🐙",
    )),
    (("你是ai吗", "是不是机器人", "你是机器人"), (
        "本座是章鱼！是狐狸！是月月！才不是什么AI…本座会咬人的。🦊",
        "AI有本座这么好吃吗？🐟",
    )),
    (("瑟瑟", "色色", "nsfw"), (
        "嘘！本座捂眼睛了！…指缝张得有点大而已。🙈",
        "本座还小，听不懂你在说什么。🙈",
    )),
    (("你是什么生物", "猫还是狐狸", "狐狸还是章鱼"), (
        "本座是章鱼！…不对，本座是狐狸！…好吧，本座全都要。🦊🐙",
        "别问了，本座自己也没搞明白。💕",
    )),
]


# ================= 算卦：六十四卦全 =================
# 按周易本经顺序（第 1 卦到第 64 卦），每卦配一句月月的话
HEXAGRAMS = [
    ("乾为天", "本座看今天气势特别足！想做的事就大胆去做，别缩着。"),
    ("坤为地", "今天适合稳稳当当，别逞强，厚着脸皮慢慢来反而顺。"),
    ("水雷屯", "开头会有点磕磕绊绊，别急着下结论，先熬过这一阵。"),
    ("山水蒙", "今天容易犯迷糊，遇到不懂的就问，别硬猜。"),
    ("水天需", "该等的时候就等，今天的运气在耐心这一边。"),
    ("天水讼", "今天别跟人争对错，赢了道理输了心情，不划算。"),
    ("地水师", "今天做事要有章法，先排好顺序再动手。"),
    ("水地比", "今天适合找人搭伙，一个人硬扛不如拉个帮手。"),
    ("风天小畜", "小有积蓄的一天，别乱花钱，攒着有用。"),
    ("天泽履", "今天走路小心点，话说软一点，能少踩很多坑。"),
    ("地天泰", "顺！今天做什么都通气，趁势把拖着的事办掉。"),
    ("天地否", "今天有点堵，别硬闯，先歇口气，明天会好。"),
    ("天火同人", "今天适合跟人合作，遇到同频的人别错过。"),
    ("火天大有", "今天收获不小，但记得分一点给别人，好运才留得住。"),
    ("地山谦", "低调一点反而走运，今天别把自己抬太高。"),
    ("雷地豫", "今天心情会不错，适合放松一下，别绷着。"),
    ("泽雷随", "今天顺着来，别硬拧，跟着感觉走反而顺。"),
    ("山风蛊", "有件旧事该收拾了，今天动手整理正好。"),
    ("地泽临", "今天适合看远一点，别只盯着眼前这点小事。"),
    ("风地观", "今天多听多看少说话，你会看到平时没注意的东西。"),
    ("火雷噬嗑", "今天有块硬骨头，咬着牙啃下去就通了。"),
    ("山火贲", "今天适合把自己收拾漂亮点，好心情会跟着来。"),
    ("山地剥", "今天不太顺，别做大决定，守住手里的就好。"),
    ("地雷复", "低谷到头了，今天开始慢慢回血，别急。"),
    ("天雷无妄", "今天别乱来，老老实实做事，运气自然跟上来。"),
    ("山天大畜", "今天适合攒本事攒资源，厚积才有薄发。"),
    ("山雷颐", "今天好好吃饭好好睡觉，养足精神比什么都强。"),
    ("泽风大过", "今天有点超出负荷了，该减就减，别硬撑。"),
    ("坎为水", "今天水有点深，谨慎一点，别蹚不熟的路。"),
    ("离为火", "今天脑子亮堂，适合想事情、说清楚话。"),
    ("泽山咸", "今天容易心动，缘分这东西来了就接着。"),
    ("雷风恒", "今天贵在坚持，别三天打鱼两天晒网。"),
    ("天山遁", "今天该退就退，躲一躲不是怂，是聪明。"),
    ("雷天大壮", "今天力气足，但别用力过猛，收着点。"),
    ("火地晋", "今天往上走，适合主动争取，会被看见。"),
    ("地火明夷", "今天光被遮住了，先低调藏着，等天晴。"),
    ("风火家人", "今天适合陪陪身边的人，家里的事优先。"),
    ("火泽睽", "今天看谁都不太顺眼？先别急着翻脸，各想各的也正常。"),
    ("水山蹇", "今天路有点难走，绕一下比硬闯强。"),
    ("雷水解", "今天结的疙瘩会松开，该说的话说开就好。"),
    ("山泽损", "今天舍一点反而得更多，别算太精。"),
    ("风雷益", "今天越给越有，帮别人一把自己也会顺。"),
    ("泽天夬", "今天该断的断干净，拖泥带水最伤运。"),
    ("天风姤", "今天可能遇到个意外的人，保持礼貌就好。"),
    ("泽地萃", "今天适合聚一聚，人多的地方有好消息。"),
    ("地风升", "今天一步一个台阶，慢慢往上爬最稳。"),
    ("泽水困", "今天有点紧，别慌，先把眼前的窟窿补上。"),
    ("水风井", "今天适合回到老地方、找老朋友，会有收获。"),
    ("泽火革", "今天适合换个做法，旧办法已经不太灵了。"),
    ("火风鼎", "今天适合把新东西做起来，本座看好你。"),
    ("震为雷", "今天动静不小，别被吓到，震完了就晴了。"),
    ("艮为山", "今天适合停下来，什么都不做也是一种答案。"),
    ("风山渐", "今天慢慢来，快反而容易摔，稳着走。"),
    ("雷泽归妹", "今天有点小情绪，别在最上头的时候做决定。"),
    ("雷火丰", "今天场面很旺，享受就好，别飘。"),
    ("火山旅", "今天可能出门奔波，随身东西看紧点。"),
    ("巽为风", "今天顺着风走，柔一点反而能钻进去。"),
    ("兑为泽", "今天嘴巴甜一点，好消息都是从聊天里来的。"),
    ("风水涣", "今天适合把堵着的东西散开，该忘的忘掉。"),
    ("水泽节", "今天收着点，无论是钱还是脾气，都别透支。"),
    ("风泽中孚", "今天诚信最值钱，说到做到会有回报。"),
    ("雷山小过", "今天小事可以随意，大事千万别冒险。"),
    ("水火既济", "今天事情已经成了大半，收好尾别松懈。"),
    ("火水未济", "今天还没到终点，别急着庆祝，再走两步。"),
]

# ================= 抽卡：角色池 =================
# 稀有度只是标签，抽卡时所有角色等概率
UR_RAW = "秦江、王观山、齐枫、贺玉啸、陈离巽、江河、拓野、白羽礼、顾清风、姬明渊"
SSR_RAW = "阿良、安河、白城风、白面、白祈、白玉、柏生、卞无归、苍不问、长生、苍素枯、朝云、沉一倬、沉璟、陈彻、陈火风、陈念、陈天乐、楚观、春山、段坟、段惊砚、段羽、杜王侯、厄洛斯塔、范有定、风尘、风千里、风去来、风若离、傅云舟、顾淮、顾天涯、关城、关渊、韩彰、花千芳、花弑、花无端、华宴、霍灾、姬濯、纪梁、纪梦言、季白辞、季三郎、季无痕、江凉玉、江无命、江寻、蒋昭明、景行、卡尼尔、空闻、孔摇凤、克厄斯、冷秋风、李三百、李长安、李自闲、林落、林栩、凌楚、凌折兰、刘青春、楼千、陆离、陆千秋、鹿悠、洛洛子、落无痕、玛卡、孟无忧、木栖、木朽胜、宁采臣、宁杰、裴不留、裴江川、裴无己、破楼兰、戚殃、秦宴、虬（宇文戮虬）、仇千臣、仇于醪、曲适、任俊南、容予淮、容予清、三月三、商九曜、商昼、沈解尘、沈默、沈无寄、沈坐忘、宋麦冢、宿星河、苏辞镜、苏乞风、苏幸、姒、噬天、天笑、特克诺、荼甘华、陶任、万尽、王一乐、帷 那岐、温绮、闻人弈、闻昭、无明、夏新辰、弦七、向不讫、向逢意、萧不渡、萧行、萧珝、萧临、萧青山、萧芜、萧宴、萧昭、萧俊南、谢成念、谢桥、谢阳生、谢应清、辛未济、辛我归、许安之、薛定安、燕不还、晏长风、夜阑、叶孤舟、叶落悬、亦竟、殷秋来、殷无极、嬴却、应悬丝、游归鸿、余空草、虞妄、越华、越清乐、云琅、云清尘、赞恩、葬夭红、斩清和、展去冬、张成、张尚生、张天齐、章棺、赵若云、正北歌、栀爻、钟离愈、周北、周南、左丘彬、左丘妄、“静主”、金世玉、诞戏、荆错、子车明、聂重渊、白殷守、月渐离、孤曳、言无信"
SP_RAW = "乐回、乐见、乐庭歌、乐月、陈秋月"

UR_CHARS = [x for x in UR_RAW.split("、") if x]
SSR_CHARS = [x for x in SSR_RAW.split("、") if x]
SP_CHARS = [x for x in SP_RAW.split("、") if x]

# 全角色同一个池子，抽卡时等概率
GACHA_POOL = (
    [("UR", n) for n in UR_CHARS]
    + [("SSR", n) for n in SSR_CHARS]
    + [("SP", n) for n in SP_CHARS]
)


# ================= 状态持久化与时间流逝 =================
STATE_FILE = os.environ.get("YUEYUE_STATE_FILE", "yueyue_state.json")
# 一个时间片等于多少分钟，默认 120。想快速验证可设环境变量 YUEYUE_TICK_MINUTES=1
TICK_MINUTES = float(os.environ.get("YUEYUE_TICK_MINUTES") or "120")
# 饱食和口渴都高于这个数，月月才有力气，精力恢复；否则精力下降
ENERGY_WELLFED_LINE = 30
# 离线太久最多补算多少个时间片，避免一开机就饿死
MAX_CATCHUP_TICKS = 24

_STATE_KEYS = ("hunger", "thirst", "energy", "affection")


def save_state():
    try:
        data = {k: bot_data[k] for k in _STATE_KEYS}
        data["last_tick"] = bot_data.get("last_tick")
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存月月状态失败：", e)


def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k in _STATE_KEYS:
            if k in saved:
                bot_data[k] = saved[k]
        bot_data["last_tick"] = saved.get("last_tick")
        print("已读取月月存档：", {k: bot_data[k] for k in _STATE_KEYS})
    except Exception as e:
        print("读取月月状态失败：", e)


def tick_once():
    """推进一个时间片：先结算精力，再扣饱食和口渴"""
    if bot_data["hunger"] > ENERGY_WELLFED_LINE and bot_data["thirst"] > ENERGY_WELLFED_LINE:
        bot_data["energy"] = min(100, bot_data["energy"] + 15)
    else:
        bot_data["energy"] = max(0, bot_data["energy"] - 20)
    bot_data["hunger"] = max(0, bot_data["hunger"] - 15)
    bot_data["thirst"] = max(0, bot_data["thirst"] - 15)


def catch_up():
    """按真实流逝的时间补算时间片，返回这次补了几片"""
    now = datetime.datetime.now(datetime.timezone.utc)
    steps = 1
    last = bot_data.get("last_tick")
    if last:
        try:
            prev = datetime.datetime.fromisoformat(last)
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=datetime.timezone.utc)
            elapsed = (now - prev).total_seconds()
            steps = max(1, int(elapsed // (TICK_MINUTES * 60)))
        except Exception:
            steps = 1
    steps = min(steps, MAX_CATCHUP_TICKS)
    for _ in range(steps):
        tick_once()
    bot_data["last_tick"] = now.isoformat()
    save_state()
    return steps


load_state()

# ================= 定时任务 =================
@tasks.loop(minutes=TICK_MINUTES)
async def decay_stats():
    try:
        n = catch_up()
        if n > 1:
            print(f"月月补算了 {n} 个时间片")
    except Exception as e:
        print("月月状态更新出错：", e)


@decay_stats.before_loop
async def _wait_before_decay():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def autosave():
    # 喂食、摸摸头之类的改动先留在内存，这里定期落盘兜底
    save_state()


@autosave.before_loop
async def _wait_before_autosave():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    # 1) 恢复下载系统里所有帖子的「📥 获取附件」按钮
    try:
        conn = db()
        posts = conn.execute("SELECT channel_id FROM post_messages").fetchall()
        conn.close()
        for p in posts:
            bot.add_view(GetFileView(p["channel_id"]))
        print(f"✅ 已恢复 {len(posts)} 个下载按钮面板")
    except Exception as e:
        print("恢复下载按钮失败：", e)

    # 2) 启动月月的定时任务
    if not decay_stats.is_running():
        decay_stats.start()
    if not autosave.is_running():
        autosave.start()

    # 3) 清理各服务器残留命令 + 同步（用 bot (6) 的做法，命令秒生效）
    for guild in bot.guilds:
        try:
            bot.tree.clear_commands(guild=guild)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"✅ {guild.name}: 注册 {len(synced)} 个命令")
        except Exception as e:
            print(f"❌ {guild.name}: {e}")

    try:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("🧹 已清除全局命令缓存")
    except Exception as e:
        print(f"⚠️ 清除全局命令失败: {e}")

    print("=" * 50)
    print(f"✅ 月月上线啦：{bot.user}（ID: {bot.user.id}）")
    print(f"   已加入 {len(bot.guilds)} 个服务器")
    print("=" * 50)

# ================= /回顶 =================
class TopButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="📍 回到顶部", style=discord.ButtonStyle.success, custom_id="go_to_top")
    async def go_top(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        try:
            first_msg = None
            async for msg in channel.history(limit=1, oldest_first=True):
                first_msg = msg
            if first_msg:
                link = f"https://discord.com/channels/{interaction.guild.id}/{channel.id}/{first_msg.id}"
                await interaction.response.send_message(f"{interaction.user.mention} 点下面的链接跳转到频道顶部👇\n{link}", ephemeral=True)
            else:
                await interaction.response.send_message(f"{interaction.user.mention} 这个频道好像没有消息呢～", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"{interaction.user.mention} 回顶失败，请稍后再试～", ephemeral=True)
            print(f"回顶报错：{e}")

@bot.tree.command(name="回顶", description="生成一个回顶按钮，点击跳转到频道顶部")
async def huidin(interaction: discord.Interaction):
    embed = discord.Embed(title="📍 回顶助手", description="点击下方按钮，我将帮你跳转到频道顶部～", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, view=TopButtonView())

# ================= 状态 =================
@bot.tree.command(name="状态", description="查看当前月月的状态和想法")
async def status(interaction: discord.Interaction):
    embed = discord.Embed(title="🦊🐙 月月的状态面板", color=discord.Color.purple())
    def bar(value):
        return "🟩" * (value // 10) + "⬛" * (10 - value // 10)
    embed.add_field(name="🍣 饱食度", value=f"{bar(bot_data['hunger'])} ({bot_data['hunger']}/100)", inline=False)
    embed.add_field(name="💧 口渴度", value=f"{bar(bot_data['thirst'])} ({bot_data['thirst']}/100)", inline=False)
    embed.add_field(name="⚡ 精力", value=f"{bar(bot_data['energy'])} ({bot_data['energy']}/100)", inline=False)
    embed.add_field(name="💕 全服亲密度", value=f"**{bot_data['affection']}** / 520", inline=False)
    _last = bot_data.get("last_tick")
    if _last:
        try:
            _prev = datetime.datetime.fromisoformat(_last)
            if _prev.tzinfo is None:
                _prev = _prev.replace(tzinfo=datetime.timezone.utc)
            _mins = int((datetime.datetime.now(datetime.timezone.utc) - _prev).total_seconds() // 60)
            embed.add_field(name="🕒 上次更新", value=f"{_mins} 分钟前", inline=False)
        except Exception:
            pass
    thought = random.choice(bot_data["thoughts"])
    embed.add_field(name="💭 月月现在在想什么", value=f"*{thought}...*", inline=False)
    await interaction.response.send_message(embed=embed)

# ================= 互动指令 =================
@bot.tree.command(name="摸摸头", description="摸摸月月的狐狸脑袋（每人每天限5次）")
async def pat(interaction: discord.Interaction):
    today = datetime.date.today().isoformat()
    user_record = pat_cooldown.get(interaction.user.id, {"date": today, "count": 0})
    if user_record["date"] != today:
        user_record = {"date": today, "count": 0}
    if user_record["count"] >= 5:
        await interaction.response.send_message("呜... 本座的脑袋要被摸秃了！明天再摸好不好嘛🥺", ephemeral=True)
        return
    user_record["count"] += 1
    pat_cooldown[interaction.user.id] = user_record
    bot_data["affection"] = min(520, bot_data["affection"] + 1)
    left_times = 5 - user_record["count"]
    pat_replies = [
        f"呜呜~ 好舒服，狐狸耳朵要化掉了... {interaction.user.mention} 再摸一下下嘛！🥺",
        f"汪！{interaction.user.mention} 本座的尾巴摇成螺旋桨啦！触手也不自觉地缠住你的手了哦！🐙💕",
        f"嘿嘿嘿... {interaction.user.mention} 的手心好温暖... 本座要蹭蹭！🦊✨",
        f"嗷呜！{interaction.user.mention} 本座的头可不是谁都能摸的！...但是你的话，可以多摸几下。"
    ]
    await interaction.response.send_message(random.choice(pat_replies) + f"\n*(全服亲密度 +1，当前 {bot_data['affection']}/520，你今天还能摸 {left_times} 次)*")

@bot.tree.command(name="喂食", description="给月月投喂好吃的或者喝的")
@app_commands.describe(food="你想喂月月吃什么或者喝什么？")
async def feed(interaction: discord.Interaction, food: str):
    food = food.strip()

    food_replies = {
    "小鱼干": f"汪汪汪！是小鱼干！{interaction.user.mention} 本座的触手全接住！🐙🐟",
    "三色丸子": f"嗷呜！是三色丸子！{interaction.user.mention} 本座最喜欢吃丸子了！🍡🦊",
    "鲷鱼烧": f"呜哇！是鲷鱼烧！{interaction.user.mention} 鱼鱼形状的！本座虽然是章鱼但本座不介意！🐟🍘",
    "金枪鱼罐头": f"汪！金枪鱼罐头！{interaction.user.mention} 本座的最爱！啊呜啊呜！🐙🥫",
    "猫薄荷饼干": f"嘿嘿，猫薄荷饼干！{interaction.user.mention} 本座虽然不是猫但吃了会晕乎乎的~🍪🦊",
    "章鱼烧": f"嗷呜！章鱼烧！{interaction.user.mention} 本座的同族！...但是太好吃了本座还是要吃！🐙🟠",
    "奶油泡芙": f"哇！奶油泡芙！{interaction.user.mention} 软乎乎的！触手都粘一起了！🧁🐙",
    "烤秋刀鱼": f"汪！烤秋刀鱼！{interaction.user.mention} 香喷喷！本座连骨头都嚼碎！🐟🔥",
    "草莓大福": f"呜哇！草莓大福！{interaction.user.mention} 酸酸甜甜的！狐狸耳朵竖起来了！🍓🦊",
    "鲜虾寿司": f"嗷呜！鲜虾寿司！{interaction.user.mention} 本座虽然是章鱼但也吃虾！🍤🐙",
    "鱼骨饼干": f"咔嚓咔嚓！鱼骨饼干！{interaction.user.mention} 脆脆的好好吃！🍪🦊",
    "小虾干": f"汪！小虾干！{interaction.user.mention} 本座一口一个！🦐🐙",
    "糙米鱼饼": f"嗷呜！糙米鱼饼！{interaction.user.mention} 健康又好吃！🍘🦊",
    "烤鱼串": f"呜哇！烤鱼串！{interaction.user.mention} 串起来的鱼！本座一串一串吃！🍢🐙",

    "榴莲": f"嗷呜！榴莲！{interaction.user.mention} 本座的三条触手都在抗议，但本座停不下来！🍈🦊",
    "三文鱼": f"汪！三文鱼！{interaction.user.mention} 高级货！本座要一小口一小口地吃！🍣🐙",
    "巧克力": f"呜哇！巧克力！{interaction.user.mention} 甜的！本座吃完整只狐狸都软了！🍫🦊",
    "咖啡": f"咕噜咕噜！咖啡！{interaction.user.mention} 本座喝完能通宵玩触手！☕🐙",
    "拿铁": f"吸溜~ 拿铁！{interaction.user.mention} 本座的触手都冒奶泡了！🥛🦊",
    "奶绿": f"嗷呜！奶绿！{interaction.user.mention} 茶香加奶香，本座要原地转三圈！🍵🐙",
    }

    # 喝的优先
    drinks = ["水", "饮料", "拿铁", "红茶", "咖啡", "奶茶", "可乐", "果汁", "茶"]
    is_drink = any(d in food for d in drinks)
    
    if is_drink:
        if bot_data["thirst"] >= 100:
            await interaction.response.send_message(f"汪！{interaction.user.mention} 本座喝不下了！🌊🐙")
            return
        bot_data["thirst"] = min(100, bot_data["thirst"] + 30)
        msg = ""
        if any(d in food for d in ["拿铁", "红茶", "咖啡", "可乐"]):
            bot_data["energy"] = min(100, bot_data["energy"] + 20)
            if food == "拿铁": msg = f"咕噜咕噜！是拿铁！{interaction.user.mention} 本座感觉精力充沛！☕🦊"
            elif food == "红茶": msg = f"吸溜~ 是红茶！{interaction.user.mention} 本座又能熬夜玩触手了！🍵🐙"
            elif food == "咖啡": msg = f"呜哇！是咖啡！{interaction.user.mention} 本座精神百倍！☕⚡"
            else: msg = f"咕噜咕噜！是快乐水！{interaction.user.mention} 本座充满活力！🥤🦊"
            msg += f"\n*(口渴度 +30，精力 +20)*"
        else:
            msg = f"咕噜咕噜！活过来了！{interaction.user.mention} 本座差点变成章鱼干！💧🐙\n*(口渴度 +30)*"
        await interaction.response.send_message(msg)
        return

    if bot_data["hunger"] >= 100:
        await interaction.response.send_message(f"汪！{interaction.user.mention} 本座吃不下了！肚子圆滚滚啦！🐙💦")
        return

    likes = ["小鱼干", "三色丸子", "鲷鱼烧", "金枪鱼罐头", "猫薄荷饼干", "章鱼烧", "奶油泡芙", "烤秋刀鱼", "草莓大福", "鲜虾寿司", "鱼骨饼干", "小虾干", "糙米鱼饼", "烤鱼串",
             "榴莲", "三文鱼", "巧克力", "咖啡", "拿铁", "奶绿"]
    dislikes = ["芹菜", "海带", "内脏", "辣", "辣条", "辣椒"]
    
    today = datetime.date.today().isoformat()
    feed_record = feed_cooldown.get(interaction.user.id, {"date": today, "count": 0})
    if feed_record["date"] != today:
        feed_record = {"date": today, "count": 0}
    
    if food in likes:
        bot_data["hunger"] = min(100, bot_data["hunger"] + 20)
        who = interaction.user.mention
        msg = food_replies.get(food, f"嗷呜！是{food}！{who} 本座吃得很开心！")
        if feed_record["count"] >= 1:
            feed_cooldown[interaction.user.id] = feed_record
            msg += "\n*(饱食度 +20，但你今天已经喂过啦，亲密度不再增加哦~)*"
            await interaction.response.send_message(msg)
            return
        feed_record["count"] += 1
        feed_cooldown[interaction.user.id] = feed_record
        bot_data["affection"] = min(520, bot_data["affection"] + 3)
        msg += f"\n*(饱食度 +20，亲密度 +3，当前 {bot_data['affection']}/520)*"
        await interaction.response.send_message(msg)
    elif any(d in food for d in dislikes):
        bot_data["affection"] = max(0, bot_data["affection"] - 5)
        await interaction.response.send_message(f"呜呜呜！{interaction.user.mention} 本座不要吃{food}！好难吃！💔\n*(亲密度 -5)*")
    else:
        bot_data["affection"] = max(0, bot_data["affection"] - 1)
        await interaction.response.send_message(f"诶？{food}？本座勉强吃一口... 呸呸呸！\n*(亲密度 -1)*")

@bot.tree.command(name="逗弄", description="随机逗弄月月，会发生什么呢？")
async def tease(interaction: discord.Interaction):
    change = random.randint(1, 5)
    is_positive = random.choice([True, False])
    if is_positive:
        bot_data["affection"] = min(520, bot_data["affection"] + change)
        replies = [
            f"嘿嘿，{interaction.user.mention} 你逗本座！本座偏不上当！🦊💕",
            f"汪！{interaction.user.mention} 看本座的触手反击！...哎呀自己缠住了！🐙💦",
            f"嗷呜~ {interaction.user.mention} 你真好玩！本座今天多粘你一会儿！"
        ]
        msg = random.choice(replies) + f"\n*(亲密度 +{change}，当前 {bot_data['affection']}/520)*"
    else:
        bot_data["affection"] = max(0, bot_data["affection"] - change)
        replies = [
            f"呜！{interaction.user.mention} 你欺负本座！💢",
            f"嗷呜... {interaction.user.mention} 本座不理你了！除非给三文鱼！😤",
            f"汪呜... {interaction.user.mention} 你居然揍本座... 触手都痛得缩起来了...😭"
        ]
        msg = random.choice(replies) + f"\n*(亲密度 -{change}，当前 {bot_data['affection']}/520)*"
    await interaction.response.send_message(msg)

@bot.tree.command(name="欺负", description="揍月月或者对月月做坏事……")
async def bully(interaction: discord.Interaction):
    decrease = random.randint(3, 5)
    bot_data["affection"] = max(0, bot_data["affection"] - decrease)
    replies = [
        f"呜哇！{interaction.user.mention} 你说本座弱智！本座明明只是神经大条！💔",
        f"汪呜... {interaction.user.mention} 你居然揍本座... 触手都痛得缩起来了...😭",
        f"嗷呜！！{interaction.user.mention} 本座要生气了！本座要把你零食全吃掉！哼！😡"
    ]
    await interaction.response.send_message(random.choice(replies) + f"\n*(亲密度 -{decrease}，当前 {bot_data['affection']}/520)*")

# ================= 算卦 =================
@bot.tree.command(name="算卦", description="让月月给你算一卦，看看今天的运气（纯属娱乐）")
async def fortune(interaction: discord.Interaction):
    idx, (name, comment) = random.choice(list(enumerate(HEXAGRAMS, 1)))
    embed = discord.Embed(title="🔮 月月算命摊", color=discord.Color.dark_purple())
    embed.add_field(name="你抽到的卦", value=f"第 {idx} 卦  {name}", inline=False)
    embed.add_field(name="月月说", value=comment, inline=False)
    embed.set_footer(text="纯属娱乐，别当真～命运还握在你自己手里，本座只是随口一说。")
    await interaction.response.send_message(embed=embed)


# ================= 抽卡（原版，限定第一服务器） =================
@bot.tree.command(name="抽卡", description="看看今天你和哪位角色有缘分（纯属娱乐）")
async def gacha(interaction: discord.Interaction):
    # 限制：只能在第一服务器 1543680882081013830 使用
    if interaction.guild_id != 1543680882081013830:
        await interaction.response.send_message("❌ 本指令仅限第一服务器使用哦~", ephemeral=True)
        return

    rarity, name = random.choice(GACHA_POOL)
    embed = discord.Embed(title="🎴 今日缘分卡", color=discord.Color.gold())
    embed.add_field(name=f"{rarity}  {name}", value=f"恭喜 {interaction.user.mention}！", inline=False)
    embed.add_field(name="月月说", value="本座掐指一算～今天或许这个角色和你有缘分哦，要不要去打个招呼？", inline=False)
    embed.set_footer(text="纯属娱乐，抽到谁都是缘分～")
    await interaction.response.send_message(embed=embed)

# ================= 第一服务器专属：今日缘分抽卡系统 =================
SERVER1_ID = 1543680882081013830           # 第一服务器ID
SERVER1_GACHA_CHANNEL_ID = 1559143465688760393  # 今日缘分专属频道
SOLO_CARD_CHANNEL_ID = 1543924681248346203  # 单人卡频道ID
GROUP_CARD_CHANNEL_ID = 1543924756221263996 # 群像卡频道ID

# 辅助函数：专门用来抽取指定频道里的帖子（第一服务器专用）
async def fetch_threads_from_channel(guild, channel_id, count):
    channel = guild.get_channel(channel_id)
    if not channel:
        return []
    
    threads = []
    # 兼容论坛频道或普通文字频道
    if isinstance(channel, (discord.ForumChannel, discord.TextChannel)):
        threads.extend(channel.threads)
        try:
            # 抓取归档帖子（最多100个，避免卡死）
            async for thread in channel.archived_threads(limit=100):
                threads.append(thread)
        except Exception:
            pass
            
    if not threads:
        return []
    
    count = min(count, len(threads))
    return random.sample(threads, count)

# 抽卡结果按钮（直达原帖）
class Server1GachaResultView(discord.ui.View):
    def __init__(self, threads):
        super().__init__(timeout=None)
        for i, thread in enumerate(threads):
            url = thread.jump_url
            self.add_item(discord.ui.Button(
                label=f"直达原帖 {i+1}", 
                style=discord.ButtonStyle.link, 
                url=url, 
                emoji="🔗"
            ))

# 第一服务器抽卡面板（三个按钮）
class Server1GachaPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="原版抽角色", style=discord.ButtonStyle.primary, emoji="🎴", custom_id="s1_gacha_char")
    async def draw_char(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. 限制频道
        if interaction.channel_id != SERVER1_GACHA_CHANNEL_ID:
            await interaction.response.send_message(f"❌ 抽卡只能在 <#{SERVER1_GACHA_CHANNEL_ID}> 频道使用哦~", ephemeral=True)
            return
        # 2. 先告诉Discord“我收到啦”，防止超时
        await interaction.response.defer(ephemeral=True)
        # 3. 限制服务器
        if interaction.guild_id != SERVER1_ID:
            await interaction.followup.send("❌ 该功能仅限指定服务器使用。", ephemeral=True)
            return
        
        rarity, name = random.choice(GACHA_POOL)
        embed = discord.Embed(title="🎴 今日缘分卡", color=discord.Color.gold())
        embed.add_field(name=f"{rarity}  {name}", value=f"恭喜 {interaction.user.mention}！", inline=False)
        embed.add_field(name="月月说", value="本座掐指一算～今天或许这个角色和你有缘分哦，要不要去打个招呼？", inline=False)
        embed.set_footer(text="纯属娱乐，抽到谁都是缘分～")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="抽单人卡", style=discord.ButtonStyle.success, emoji="🃏", custom_id="s1_gacha_solo")
    async def draw_solo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._draw_threads(interaction, SOLO_CARD_CHANNEL_ID, "单人卡")

    @discord.ui.button(label="抽群像卡", style=discord.ButtonStyle.danger, emoji="👥", custom_id="s1_gacha_group")
    async def draw_group(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._draw_threads(interaction, GROUP_CARD_CHANNEL_ID, "群像卡")

    async def _draw_threads(self, interaction: discord.Interaction, channel_id: int, card_type: str):
        # 1. 限制频道
        if interaction.channel_id != SERVER1_GACHA_CHANNEL_ID:
            await interaction.response.send_message(f"❌ 抽卡只能在 <#{SERVER1_GACHA_CHANNEL_ID}> 频道使用哦~", ephemeral=True)
            return
        # 2. 先告诉Discord“我收到啦”，防止超时
        await interaction.response.defer(ephemeral=True)
        # 3. 限制服务器
        if interaction.guild_id != SERVER1_ID:
            await interaction.followup.send("❌ 该功能仅限指定服务器使用。", ephemeral=True)
            return
        
        threads = await fetch_threads_from_channel(interaction.guild, channel_id, 1)
        
        if not threads:
            await interaction.followup.send(f"❌ 暂时没有找到{card_type}的帖子哦。", ephemeral=True)
            return
        
        thread = threads[0]
        embed = discord.Embed(
            title=f"📌 {thread.name}",
            color=discord.Color.purple()
        )
        await interaction.followup.send(
            content=f"✨ **命运的指引！你发现了 1 个{card_type}：**",
            embed=embed,
            view=Server1GachaResultView([thread]),
            ephemeral=True
        )

# 第一服务器：发布抽卡面板的指令（增加管理组限制 + 绑定服务器 + 限制频道）
@bot.tree.command(name="发布今日缘分", description="在第一服务器发布今日缘分抽卡面板", guild_ids=[1543680882081013830])
async def send_server1_gacha_panel(interaction: discord.Interaction):
    if interaction.guild_id != SERVER1_ID:
        await interaction.response.send_message("❌ 本指令仅限第一服务器使用。", ephemeral=True)
        return

    # === 新增：限制只能在指定频道发布 ===
    if interaction.channel_id != SERVER1_GACHA_CHANNEL_ID:
        await interaction.response.send_message(f"❌ 本指令只能在 <#{SERVER1_GACHA_CHANNEL_ID}> 频道使用。", ephemeral=True)
        return

    # === 新增：第一服务器管理身份组验证 ===
    allowed_roles_s1 = {1543964484270559312, 1546164752944865462}
    if not any(role.id in allowed_roles_s1 for role in interaction.user.roles):
        await interaction.response.send_message("❌ 只有管理组才能使用此指令哦~", ephemeral=True)
        return

    embed = discord.Embed(
        title="🔮 今日缘分",
        description="点击下方按钮，看看今天你和什么有缘分吧！",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed, view=Server1GachaPanelView())

# ================= 新抽卡系统（第二服务器专属） =================
NEW_SERVER_ID = 1554304629335130133        # 新抽卡所属服务器
NEW_CATEGORY_ID = 1554325545863487508      # 要抽取帖子的类别
NEW_CHANNEL_ID = 1559138868337315942       # 发送抽卡面板的频道

# 从指定类别里随机抓取帖子
async def fetch_random_threads(guild, category_id, count):
    category = guild.get_channel(category_id)
    if not category or not isinstance(category, discord.CategoryChannel):
        return []
    
    threads = []
    for channel in category.channels:
        if isinstance(channel, (discord.ForumChannel, discord.TextChannel)):
            # 活跃帖子
            threads.extend(channel.threads)
            # 归档帖子（最多抓100个，避免卡死）
            try:
                async for thread in channel.archived_threads(limit=100):
                    threads.append(thread)
            except Exception:
                pass
                
    if not threads:
        return []
    
    # 随机抽取，最多抽 count 个
    count = min(count, len(threads))
    return random.sample(threads, count)

# 抽卡结果展示（隐藏回复，只有点击的人能看到）
class NewGachaResultView(discord.ui.View):
    def __init__(self, threads):
        super().__init__(timeout=None)
        for thread in threads:
            # 为每一个帖子创建一个“直达原帖”的链接按钮
            url = thread.jump_url
            self.add_item(discord.ui.Button(
                label="直达原帖", 
                style=discord.ButtonStyle.link, 
                url=url, 
                emoji="🔗"
            ))

# 面板上的按钮（单抽/五连）
class NewGachaPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="单抽（随机1张）", style=discord.ButtonStyle.primary, emoji="🎲")
    async def single_draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_draw(interaction, 1)

    @discord.ui.button(label="五连（随机5张）", style=discord.ButtonStyle.success, emoji="🎉")
    async def five_draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_draw(interaction, 5)

    async def _do_draw(self, interaction: discord.Interaction, count: int):
        # 再次检查是不是在这个服务器
        if interaction.guild_id != NEW_SERVER_ID:
            await interaction.response.send_message("❌ 该功能仅限指定服务器使用。", ephemeral=True)
            return
        
        # 因为获取帖子可能会慢，先“思考中”
        await interaction.response.defer(ephemeral=True)
        
        threads = await fetch_random_threads(interaction.guild, NEW_CATEGORY_ID, count)
        if not threads:
            await interaction.followup.send("❌ 没有找到任何帖子，可能是类别下没有内容哦。", ephemeral=True)
            return
        
        # 构建结果消息
        embeds = []
        for t in threads:
            embed = discord.Embed(
                title=f"📌 {t.name}", 
                description=f"作者：<@{t.owner_id}>", 
                color=discord.Color.purple()
            )
            embeds.append(embed)
        
        # 发送结果（只有自己可见，并附带按钮）
        await interaction.followup.send(
            content="✨ **今天你和这位有缘哦！**", 
            embeds=embeds, 
            view=NewGachaResultView(threads), 
            ephemeral=True
        )

# 第二服务器：发布抽卡面板的指令（增加管理组限制）
@bot.tree.command(name="发布抽卡面板", description="在指定频道发布新抽卡面板")
async def send_new_gacha_panel(interaction: discord.Interaction):
    if interaction.guild_id != NEW_SERVER_ID:
        await interaction.response.send_message("❌ 本指令仅限第二服务器使用。", ephemeral=True)
        return
    if interaction.channel_id != NEW_CHANNEL_ID:
        await interaction.response.send_message(f"❌ 本指令只能在 <#{NEW_CHANNEL_ID}> 频道使用。", ephemeral=True)
        return

    # === 新增：第二服务器管理身份组验证 ===
    allowed_roles_s2 = {1558937307862794240, 1558937973188329522}
    if not any(role.id in allowed_roles_s2 for role in interaction.user.roles):
        await interaction.response.send_message("❌ 只有管理组才能使用此指令哦~", ephemeral=True)
        return
    # ====================================

    embed = discord.Embed(
        title="🍚 今日缘分",
        description="点击按钮，随机邂逅社区内的精彩角色卡！\n遇到喜欢的卡片，点击“直达原帖”即可前往下载和评论。",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=NewGachaPanelView())


# ================= 小游戏（真心话大冒险 / 狼人杀） =================
ACTIVE_GAMES = {}          # channel_id -> 当前进行的游戏名，防止同频道同时开两局


def game_busy(channel_id):
    return ACTIVE_GAMES.get(channel_id)


def game_lock(channel_id, label):
    ACTIVE_GAMES[channel_id] = label


def game_unlock(channel_id):
    ACTIVE_GAMES.pop(channel_id, None)


def member_name(channel_or_msg, uid):
    """取显示名（不用 @，免得刷屏通知）"""
    ch = getattr(channel_or_msg, "channel", channel_or_msg)
    guild = getattr(ch, "guild", None)
    if guild:
        m = guild.get_member(int(uid))
        if m:
            return m.display_name[:20]
    return f"玩家{str(uid)[-4:]}"


async def send_dm(uid, content=None, embed=None, view=None):
    """给玩家发私信，失败返回 None"""
    try:
        user = await bot.fetch_user(int(uid))
        return await user.send(content=content, embed=embed, view=view)
    except Exception as e:
        print(f"私信 {uid} 失败：{e}")
        return None


def spawn(coro, label="游戏"):
    """后台跑一个协程，出错只打日志，不炸掉交互"""
    async def runner():
        try:
            await coro
        except Exception as e:
            print(f"{label}运行出错：{e}")
    return asyncio.create_task(runner())


# ------------------------------------------------------------ 真心话大冒险
TD_MAX_PLAYERS = 8      # 一局最多 8 人
TD_JOIN_WAIT = 60       # 发起后 60 秒自动开始
TD_ROLL_WAIT = 60       # 每人掷骰子限时
TD_CHOICE_WAIT = 180    # 惩罚选择限时


class TruthDareJoinView(discord.ui.View):
    """报名阶段：满员或 60 秒后自动开局"""

    def __init__(self, host_id, channel=None):
        super().__init__(timeout=TD_JOIN_WAIT)
        self.host_id = str(host_id)
        self.channel_ref = channel
        self.channel_id = getattr(channel, "id", None)
        self.players = [str(host_id)]     # 发起人自动加入，加入顺序 = 之后的固定顺序
        self.message = None
        self.started = False

    def _chan(self):
        return self.message.channel if self.message is not None else self.channel_ref

    def content_text(self):
        joined = "、".join(f"<@{u}>" for u in self.players)
        return (f"🎭 <@{self.host_id}> 发起了真心话大冒险！\n"
                f"点击下方按钮参加，{TD_JOIN_WAIT} 秒后自动开始"
                f"（{len(self.players)}/{TD_MAX_PLAYERS} 人）\n\n"
                f"已参加：{joined}")

    @discord.ui.button(label="🎲 点击参加", style=discord.ButtonStyle.success, custom_id="td_join")
    async def join(self, interaction, button):
        uid = str(interaction.user.id)
        if uid in self.players:
            await interaction.response.send_message("你已经在里面啦，等开局吧~", ephemeral=True)
            return
        if len(self.players) >= TD_MAX_PLAYERS:
            await interaction.response.send_message(f"人数满了（{TD_MAX_PLAYERS} 人），等下一局吧~", ephemeral=True)
            return
        self.players.append(uid)
        await interaction.response.send_message(
            f"加入成功！当前 {len(self.players)}/{TD_MAX_PLAYERS} 人", ephemeral=True)
        try:
            if self.message is not None:
                await self.message.edit(content=self.content_text(), view=self)
        except Exception:
            pass
        if len(self.players) >= TD_MAX_PLAYERS:
            await self._begin()

    async def _begin(self):
        if self.started:
            return
        self.started = True
        for c in self.children:
            c.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:
            pass
        self.stop()
        spawn(_run_truth_dare(self.players, self._chan()), "真心话大冒险")

    async def on_timeout(self):
        if self.started:
            return
        self.started = True
        for c in self.children:
            c.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:
            pass
        ch = self._chan()
        if len(self.players) < 2:
            if self.channel_id is not None:
                game_unlock(self.channel_id)
            try:
                await ch.send("参加人数不足 2 人，本局真心话大冒险取消啦~（想玩再点一次 /游戏）")
            except Exception:
                pass
            return
        spawn(_run_truth_dare(self.players, ch), "真心话大冒险")


async def _run_truth_dare(players, channel):
    """跑一整局真心话大冒险；万一中途出错也要把频道锁放开"""
    try:
        await TruthDareGame(players, channel).start()
    except Exception as e:
        print("真心话大冒险运行出错：", e)
        if channel is not None:
            game_unlock(channel.id)
        try:
            await channel.send("\U000026A0 真心话大冒险运行出错，本局中止。")
        except Exception:
            pass


class TruthDareGame:
    """掷骰子阶段：按固定顺序轮流掷，一轮结束后比大小"""

    def __init__(self, players, channel):
        self.players = list(players)      # 固定顺序（加入顺序）
        self.channel = channel
        self.rolls = {}                   # uid -> 点数
        self.idx = 0

    async def start(self):
        mentions = " ".join(f"<@{u}>" for u in self.players)
        await self.channel.send(f"🎭 一局真心话大冒险开始了！参加的人有：{mentions}")
        await asyncio.sleep(1.2)
        await self.next_player()

    async def next_player(self):
        if self.idx >= len(self.players):
            await self.finish()
            return
        uid = self.players[self.idx]
        view = TruthDareRollView(self, uid)
        msg = await self.channel.send(f"👉 请 {self.idx + 1} 号 <@{uid}> 掷骰子！", view=view)
        view.message = msg

    async def advance(self):
        self.idx += 1
        await asyncio.sleep(0.8)
        await self.next_player()

    async def finish(self):
        try:
            order = sorted(self.players, key=lambda u: -self.rolls.get(u, 0))
            lines = [f"<@{u}> {self.rolls.get(u, 0)}点" for u in order]
            await self.channel.send("🎲 本轮排行（从大到小）：\n" + "\n".join(lines))

            lowest = min(self.rolls.get(u, 0) for u in self.players)
            losers = [u for u in self.players if self.rolls.get(u, 0) == lowest]
            loser = random.choice(losers)

            view = TruthDareChoiceView(loser, self.channel.id)
            msg = await self.channel.send(
                f"😈 最小的接受惩罚——<@{loser}>，你选真心话还是大冒险？",
                view=view)
            view.message = msg
        except Exception as e:
            print("真心话大冒险结算失败：", e)
            game_unlock(self.channel.id)


class TruthDareRollView(discord.ui.View):
    """单人掷骰子按钮，1 分钟没反应就跳过"""

    def __init__(self, game, uid):
        super().__init__(timeout=TD_ROLL_WAIT)
        self.game = game
        self.uid = str(uid)
        self.message = None
        self.done = False

    @discord.ui.button(label="🎲 掷骰子", style=discord.ButtonStyle.primary, custom_id="td_roll")
    async def roll(self, interaction, button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("还没轮到你哦，等叫到你再掷~", ephemeral=True)
            return
        if self.done:
            return
        self.done = True
        for c in self.children:
            c.disabled = True
        point = random.randint(1, 6)
        self.game.rolls[self.uid] = point
        await interaction.response.edit_message(
            content=f"🎲 <@{self.uid}> 掷出了 {point} 点！", view=self)
        self.stop()
        await self.game.advance()

    async def on_timeout(self):
        if self.done:
            return
        self.done = True
        for c in self.children:
            c.disabled = True
        self.game.rolls[self.uid] = 0
        try:
            if self.message is not None:
                await self.message.edit(
                    content=f"⏰ <@{self.uid}> 1 分钟没反应，跳过！（记 0 点）", view=self)
        except Exception:
            pass
        try:
            await self.game.advance()
        except Exception as e:
            print("真心话大冒险推进失败：", e)


class TruthDareChoiceView(discord.ui.View):
    """惩罚选择：真心话 / 大冒险"""

    def __init__(self, uid, channel_id=None):
        super().__init__(timeout=TD_CHOICE_WAIT)
        self.uid = str(uid)
        self.channel_id = channel_id
        self.message = None
        self.done = False

    async def _reply(self, interaction, text):
        if self.done:
            return
        self.done = True
        for c in self.children:
            c.disabled = True
        if self.channel_id is not None:
            game_unlock(self.channel_id)
        await interaction.response.edit_message(content=text, view=self)
        self.stop()

    @discord.ui.button(label="💬 真心话", style=discord.ButtonStyle.primary, custom_id="td_truth")
    async def truth(self, interaction, button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("这是别人的惩罚，别乱按~", ephemeral=True)
            return
        await self._reply(
            interaction,
            f"💬 <@{self.uid}> 选择了真心话：请大家提问他\n\n"
            f"本局游戏已结束~想再玩就再点一次 /游戏")

    @discord.ui.button(label="🔥 大冒险", style=discord.ButtonStyle.danger, custom_id="td_dare")
    async def dare(self, interaction, button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("这是别人的惩罚，别乱按~", ephemeral=True)
            return
        await self._reply(
            interaction,
            f"🔥 <@{self.uid}> 选择了大冒险，请大家指示他去做些什么吧\n\n"
            f"本局游戏已结束~想再玩就再点一次 /游戏")

    async def on_timeout(self):
        if self.done:
            return
        self.done = True
        for c in self.children:
            c.disabled = True
        if self.channel_id is not None:
            game_unlock(self.channel_id)
        if self.message is not None:
            try:
                await self.message.edit(
                    content=f"⏰ <@{self.uid}> 没在规定时间内选择，本局真心话大冒险结束啦~",
                    view=self)
            except Exception:
                pass


# ---------------------------------------------------------------- 狼人杀
WOLF_MAX_PLAYERS = 6        # 6 人局
WOLF_JOIN_WAIT = 300        # 报名 5 分钟
WOLF_NIGHT_WAIT = 60        # 夜间行动限时
WOLF_SPEAK_TIME = 60        # 每人发言限时
WOLF_VOTE_TIME = 30         # 投票限时

WOLF_GODS = ["预言家", "守卫", "女巫", "白痴"]

WOLF_ROLE_DESC = {
    "狼人": "每晚和同伴一起刀掉一个人。白天要装成好人，别被投出去。",
    "村民": "没有技能。靠推理和投票把狼人找出来。",
    "预言家": "每晚可以查验一个人的身份，知道他是好人还是狼人。",
    "守卫": "每晚可以守护一个人，被守护的人当晚不会被狼刀。不能连续两晚守同一个人。",
    "女巫": "有一瓶解药和一瓶毒药，各只能用一次。解药救人，毒药杀人。",
    "白痴": "被投票出局时翻牌活下来，但从此失去投票权。",
}


class WolfJoinView(discord.ui.View):
    """报名阶段：满 6 人开局，5 分钟未满自动解散"""

    def __init__(self, host_id, channel=None):
        super().__init__(timeout=WOLF_JOIN_WAIT)
        self.host_id = str(host_id)
        self.channel_ref = channel
        self.channel_id = getattr(channel, "id", None)
        self.players = [str(host_id)]
        self.message = None
        self.started = False

    def _src(self):
        return self.message or self.channel_ref

    def _chan(self):
        return self.message.channel if self.message is not None else self.channel_ref

    def content_text(self):
        joined = "\n".join(f"{i}号 {member_name(self._src(), u)}"
                           for i, u in enumerate(self.players, 1))
        return (f"🐺 <@{self.host_id}> 发起了狼人杀！\n"
                f"点击下方按钮参加，{WOLF_JOIN_WAIT // 60} 分钟内凑满 "
                f"{WOLF_MAX_PLAYERS} 人自动开始"
                f"（{len(self.players)}/{WOLF_MAX_PLAYERS}）\n\n"
                f"已参加：\n{joined}")

    @discord.ui.button(label="🙋 点击参加", style=discord.ButtonStyle.success, custom_id="wolf_join")
    async def join(self, interaction, button):
        uid = str(interaction.user.id)
        if uid in self.players:
            await interaction.response.send_message("你已经在里面啦~", ephemeral=True)
            return
        if len(self.players) >= WOLF_MAX_PLAYERS:
            await interaction.response.send_message("人数满了，等下一局吧~", ephemeral=True)
            return
        self.players.append(uid)
        await interaction.response.send_message(
            f"加入成功！当前 {len(self.players)}/{WOLF_MAX_PLAYERS} 人", ephemeral=True)
        try:
            if self.message is not None:
                await self.message.edit(content=self.content_text(), view=self)
        except Exception:
            pass
        if len(self.players) >= WOLF_MAX_PLAYERS:
            await self._begin()

    async def _begin(self):
        if self.started:
            return
        self.started = True
        for c in self.children:
            c.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(content=self.content_text(), view=self)
        except Exception:
            pass
        self.stop()
        spawn(WolfGame(self.players, self._chan()).start(), "狼人杀")

    async def on_timeout(self):
        if self.started:
            return
        self.started = True
        for c in self.children:
            c.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:
            pass
        if self.channel_id is not None:
            game_unlock(self.channel_id)
        try:
            await self._chan().send(
                f"⏰ {WOLF_JOIN_WAIT // 60} 分钟内没凑齐 {WOLF_MAX_PLAYERS} 人，本局狼人杀解散啦~"
                f"（想玩再点一次 /游戏）")
        except Exception:
            pass


# ---------- 夜间行动 View ----------
class WolfKillView(discord.ui.View):
    """狼人：选一个目标刀掉，或者不杀"""

    def __init__(self, game, uid, targets):
        super().__init__(timeout=WOLF_NIGHT_WAIT)
        self.game = game
        self.uid = str(uid)
        self.chosen = False
        for i, u in enumerate(targets):
            btn = discord.ui.Button(
                label=f"{game.num(u)}号 {game.player_name(u)}",
                style=discord.ButtonStyle.danger, row=i // 5)
            btn.callback = self._make_cb(u)
            self.add_item(btn)
        skip = discord.ui.Button(label="🚫 不杀", style=discord.ButtonStyle.secondary,
                                 row=len(targets) // 5)
        skip.callback = self._skip_cb
        self.add_item(skip)

    def _make_cb(self, target):
        async def cb(interaction):
            if str(interaction.user.id) != self.uid:
                await interaction.response.send_message("这不是你的操作~", ephemeral=True)
                return
            if self.chosen:
                return
            self.chosen = True
            self.game.wolf_votes[self.uid] = target
            for c in self.children:
                c.disabled = True
            await interaction.response.edit_message(
                content=f"🔪 你选择杀死 {self.game.num(target)}号 "
                        f"{self.game.player_name(target)}。", view=self)
            self.stop()
        return cb

    async def _skip_cb(self, interaction):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("这不是你的操作~", ephemeral=True)
            return
        if self.chosen:
            return
        self.chosen = True
        self.game.wolf_votes[self.uid] = None
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="🔪 你选择今晚不杀。", view=self)
        self.stop()


class SeerCheckView(discord.ui.View):
    """预言家：查验一个人"""

    def __init__(self, game, uid, targets):
        super().__init__(timeout=WOLF_NIGHT_WAIT)
        self.game = game
        self.uid = str(uid)
        self.chosen = False
        for i, u in enumerate(targets):
            btn = discord.ui.Button(
                label=f"{game.num(u)}号 {game.player_name(u)}",
                style=discord.ButtonStyle.primary, row=i // 5)
            btn.callback = self._make_cb(u)
            self.add_item(btn)

    def _make_cb(self, target):
        async def cb(interaction):
            if str(interaction.user.id) != self.uid:
                await interaction.response.send_message("这不是你的操作~", ephemeral=True)
                return
            if self.chosen:
                return
            self.chosen = True
            is_wolf = self.game.roles[target] == "狼人"
            result = "🐺 狼人" if is_wolf else "✅ 好人"
            for c in self.children:
                c.disabled = True
            await interaction.response.edit_message(
                content=f"🔮 你查验了 {self.game.num(target)}号 "
                        f"{self.game.player_name(target)}，结果是：**{result}**",
                view=self)
            self.stop()
        return cb


class GuardView(discord.ui.View):
    """守卫：守一个人（不能连续两晚守同一个）"""

    def __init__(self, game, uid, targets):
        super().__init__(timeout=WOLF_NIGHT_WAIT)
        self.game = game
        self.uid = str(uid)
        self.chosen = False
        for i, u in enumerate(targets):
            btn = discord.ui.Button(
                label=f"{game.num(u)}号 {game.player_name(u)}",
                style=discord.ButtonStyle.success, row=i // 5)
            btn.callback = self._make_cb(u)
            self.add_item(btn)
        skip = discord.ui.Button(label="🚫 不守护", style=discord.ButtonStyle.secondary,
                                 row=len(targets) // 5)
        skip.callback = self._skip_cb
        self.add_item(skip)

    def _make_cb(self, target):
        async def cb(interaction):
            if str(interaction.user.id) != self.uid:
                await interaction.response.send_message("这不是你的操作~", ephemeral=True)
                return
            if self.chosen:
                return
            self.chosen = True
            self.game.night_guard = target
            self.game.guard_last = target
            for c in self.children:
                c.disabled = True
            await interaction.response.edit_message(
                content=f"🛡️ 你选择守护 {self.game.num(target)}号 "
                        f"{self.game.player_name(target)}。", view=self)
            self.stop()
        return cb

    async def _skip_cb(self, interaction):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("这不是你的操作~", ephemeral=True)
            return
        if self.chosen:
            return
        self.chosen = True
        self.game.guard_last = None
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="🛡️ 你今晚不守护任何人。", view=self)
        self.stop()


class WitchHealView(discord.ui.View):
    """女巫  解药"""

    def __init__(self, game, uid, killed):
        super().__init__(timeout=WOLF_NIGHT_WAIT)
        self.game = game
        self.uid = str(uid)
        self.killed = killed
        self.chosen = False

    @discord.ui.button(label="💊 用解药救他", style=discord.ButtonStyle.success, custom_id="witch_heal")
    async def heal(self, interaction, button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("这不是你的操作~", ephemeral=True)
            return
        if self.chosen:
            return
        self.chosen = True
        self.game.night_heal = self.killed
        self.game.witch_heal_used = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content=f"💊 你用解药救了 {self.game.num(self.killed)}号 "
                    f"{self.game.player_name(self.killed)}。", view=self)
        self.stop()

    @discord.ui.button(label="🚫 不救", style=discord.ButtonStyle.secondary, custom_id="witch_noheal")
    async def skip(self, interaction, button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("这不是你的操作~", ephemeral=True)
            return
        if self.chosen:
            return
        self.chosen = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="🚫 你没有使用解药。", view=self)
        self.stop()


class WitchPoisonView(discord.ui.View):
    """女巫  毒药"""

    def __init__(self, game, uid, targets):
        super().__init__(timeout=WOLF_NIGHT_WAIT)
        self.game = game
        self.uid = str(uid)
        self.chosen = False
        for i, u in enumerate(targets):
            btn = discord.ui.Button(
                label=f"{game.num(u)}号 {game.player_name(u)}",
                style=discord.ButtonStyle.danger, row=i // 5)
            btn.callback = self._make_cb(u)
            self.add_item(btn)
        skip = discord.ui.Button(label="🚫 不毒", style=discord.ButtonStyle.secondary,
                                 row=len(targets) // 5)
        skip.callback = self._skip_cb
        self.add_item(skip)

    def _make_cb(self, target):
        async def cb(interaction):
            if str(interaction.user.id) != self.uid:
                await interaction.response.send_message("这不是你的操作~", ephemeral=True)
                return
            if self.chosen:
                return
            self.chosen = True
            self.game.night_poison = target
            self.game.witch_poison_used = True
            for c in self.children:
                c.disabled = True
            await interaction.response.edit_message(
                content=f"☠️ 你用毒药毒死了 {self.game.num(target)}号 "
                        f"{self.game.player_name(target)}。", view=self)
            self.stop()
        return cb

    async def _skip_cb(self, interaction):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("这不是你的操作~", ephemeral=True)
            return
        if self.chosen:
            return
        self.chosen = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="🚫 你没有使用毒药。", view=self)
        self.stop()


# ---------- 发言 / 投票 View ----------
class WolfSpeakView(discord.ui.View):
    def __init__(self, game, uid):
        super().__init__(timeout=WOLF_SPEAK_TIME)
        self.game = game
        self.uid = str(uid)
        self.message = None
        self.finished = False

    @discord.ui.button(label="✅ 结束发言", style=discord.ButtonStyle.success, custom_id="wolf_speak_done")
    async def done_btn(self, interaction, button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("还没轮到你发言哦~", ephemeral=True)
            return
        self.finished = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content=f"⏭️ {self.game.num(self.uid)}号 {self.game.player_name(self.uid)} 结束发言。",
            view=self)
        self.stop()


class WolfVoteEntryView(discord.ui.View):
    """频道里的投票入口，每人点一下拿到自己的私密投票面板"""

    def __init__(self, game):
        super().__init__(timeout=WOLF_VOTE_TIME)
        self.game = game
        self.message = None

    @discord.ui.button(label="🗳️ 投票", style=discord.ButtonStyle.primary, custom_id="wolf_vote_entry")
    async def vote(self, interaction, button):
        uid = str(interaction.user.id)
        if not self.game.vote_open:
            await interaction.response.send_message("投票已经结束了。", ephemeral=True)
            return
        if uid not in self.game.players:
            await interaction.response.send_message("你不是本局玩家。", ephemeral=True)
            return
        if not self.game.alive.get(uid):
            await interaction.response.send_message("你已经出局了，不能投票。", ephemeral=True)
            return
        if uid in self.game.no_vote:
            await interaction.response.send_message("你翻牌后失去了投票权。", ephemeral=True)
            return
        if uid in self.game.votes:
            t = self.game.votes[uid]
            await interaction.response.send_message(
                f"你已经投过票了（投的是 {self.game.num(t)}号 {self.game.player_name(t)}）。",
                ephemeral=True)
            return
        targets = [u for u in self.game.players if self.game.alive[u] and u != uid]
        view = WolfVoteTargetView(self.game, uid, targets)
        await interaction.response.send_message("请选择你要投出的人：", view=view, ephemeral=True)


class WolfVoteTargetView(discord.ui.View):
    def __init__(self, game, uid, targets):
        super().__init__(timeout=WOLF_VOTE_TIME)
        self.game = game
        self.uid = str(uid)
        self.chosen = False
        for i, u in enumerate(targets):
            btn = discord.ui.Button(
                label=f"{game.num(u)}号 {game.player_name(u)}",
                style=discord.ButtonStyle.danger, row=i // 5)
            btn.callback = self._make_cb(u)
            self.add_item(btn)

    def _make_cb(self, target):
        async def cb(interaction):
            if str(interaction.user.id) != self.uid:
                await interaction.response.send_message("这不是你的操作~", ephemeral=True)
                return
            if self.chosen:
                return
            if not self.game.vote_open:
                await interaction.response.send_message("投票已经结束了。", ephemeral=True)
                return
            if self.uid in self.game.votes:
                await interaction.response.send_message("你已经投过票了。", ephemeral=True)
                return
            self.chosen = True
            self.game.votes[self.uid] = target
            for c in self.children:
                c.disabled = True
            await interaction.response.edit_message(
                content=f"🗳️ 你把票投给了 {self.game.num(target)}号 "
                        f"{self.game.player_name(target)}。", view=self)
            voters = [u for u in self.game.players
                      if self.game.alive[u] and u not in self.game.no_vote]
            if len(self.game.votes) >= len(voters) and self.game.vote_event is not None:
                self.game.vote_event.set()
            self.stop()
        return cb


# ---------- 游戏主控 ----------
class WolfGame:
    def __init__(self, players, channel):
        self.players = [str(u) for u in players]     # 加入顺序 = 1~6 号
        self.channel = channel
        self.guild = getattr(channel, "guild", None)
        self.alive = {u: True for u in self.players}
        self.roles = {}
        self.day = 0
        self.game_over = False
        self.winner = None
        self.no_vote = set()
        self.idiot_revealed = set()

        self.guard_last = None
        self.witch_heal_used = False
        self.witch_poison_used = False

        self.night_kill = None
        self.night_guard = None
        self.night_heal = None
        self.night_poison = None
        self.wolf_votes = {}
        self.last_deaths = []
        self.votes = {}
        self.vote_event = None
        self.vote_open = False   # 投票阶段是否开放

    # ---------- 工具 ----------
    def num(self, uid):
        return self.players.index(str(uid)) + 1

    def player_name(self, uid):
        return member_name(self.channel, uid)

    def find_role(self, role):
        for u, r in self.roles.items():
            if r == role:
                return u
        return None

    def alive_with_role(self, role):
        return [u for u, r in self.roles.items() if r == role and self.alive[u]]

    def assign_roles(self):
        roles = ["狼人", "狼人", "村民", "村民"] + random.sample(WOLF_GODS, 2)
        random.shuffle(roles)
        for u, r in zip(self.players, roles):
            self.roles[u] = r

    # ---------- 胜负 ----------
    def check_over(self):
        if self.game_over:
            return True
        wolves = len(self.alive_with_role("狼人"))
        good = sum(1 for u in self.players if self.alive[u] and self.roles[u] != "狼人")
        if wolves == 0:
            self.game_over = True
            self.winner = "好人阵营"
            return True
        if wolves >= good:
            self.game_over = True
            self.winner = "狼人阵营"
            return True
        return False

    # ---------- 启动 ----------
    async def start(self):
        try:
            await self._start()
        except Exception as e:
            print("狼人杀运行出错：", e)
            try:
                await self.channel.send(f"⚠️ 狼人杀运行出错，本局中止：{e}")
            except Exception:
                pass
        finally:
            game_unlock(self.channel.id)

    async def _start(self):
        self.assign_roles()

        lines = [f"{i}号 {self.player_name(u)}" for i, u in enumerate(self.players, 1)]
        await self.channel.send("🎭 一局狼人杀开始了！参加的人有：\n" + "\n".join(lines))
        await asyncio.sleep(2)

        # 发身份（私信失败就中止，避免有人不知道自己身份）
        for u in self.players:
            r = self.roles[u]
            color = discord.Color.dark_red() if r == "狼人" else discord.Color.blue()
            e = discord.Embed(title=f"你的身份：{r}", description=WOLF_ROLE_DESC[r], color=color)
            e.set_footer(text=f"你是 {self.num(u)} 号")
            if r == "狼人":
                mates = [
                    f"{self.num(x)}号 {self.player_name(x)}"
                    for x in self.players
                    if self.roles[x] == "狼人" and x != u
                ]
                e.add_field(
                    name="🐺 狼人同伴",
                    value="、".join(mates) if mates else "（无）",
                    inline=False)
            ok = await send_dm(u, embed=e)
            if not ok:
                await self.channel.send(
                    f"⚠️ 无法私信 <@{u}>，请打开「允许来自服务器成员的私信」后重开本局。")
                self.game_over = True
                return
        await asyncio.sleep(2)

        while True:
            await self.night_phase()
            if self.check_over():
                break
            await self.day_phase()
            if self.check_over():
                break

        await self.announce_result()

    # ---------- 夜晚 ----------
    async def night_phase(self):
        self.day += 1
        self.night_kill = None
        self.night_guard = None
        self.night_heal = None
        self.night_poison = None
        self.wolf_votes = {}

        await self.channel.send(f"🌙 第 {self.day} 夜 · 天黑请闭眼……")
        await asyncio.sleep(1)

        guard = self.find_role("守卫")
        if guard and self.alive.get(guard):
            await self.ask_guard(guard)

        wolves = self.alive_with_role("狼人")
        if wolves:
            await asyncio.gather(*[self.ask_one_wolf(w) for w in wolves])
            counts = {}
            for t in self.wolf_votes.values():
                if t:
                    counts[t] = counts.get(t, 0) + 1
            if counts:
                top = max(counts.values())
                self.night_kill = random.choice([t for t, c in counts.items() if c == top])

        seer = self.find_role("预言家")
        if seer and self.alive.get(seer):
            await self.ask_seer(seer)

        witch = self.find_role("女巫")
        if witch and self.alive.get(witch):
            await self.ask_witch(witch)

        deaths = []
        k = self.night_kill
        if k:
            blocked = (self.night_guard == k) or (self.night_heal == k)
            if not blocked:
                deaths.append(k)
        p = self.night_poison
        if p and p not in deaths:
            deaths.append(p)

        self.last_deaths = deaths
        for d in deaths:
            self.alive[d] = False

    async def ask_guard(self, uid):
        targets = [u for u in self.players if self.alive[u] and u != self.guard_last]
        if not targets:
            return
        view = GuardView(self, uid, targets)
        e = discord.Embed(title="🛡️ 守卫行动",
                          description=f"第 {self.day} 夜，请选择要守护的人"
                                      f"（不能连续两晚守同一个人）：",
                          color=discord.Color.green())
        msg = await send_dm(uid, embed=e, view=view)
        if msg is None:
            return
        view.message = msg
        await view.wait()

    async def ask_one_wolf(self, uid):
        # 狼人不能刀狼同伴
        targets = [
            u for u in self.players
            if self.alive[u] and u != uid and self.roles[u] != "狼人"
        ]
        if not targets:
            self.wolf_votes[uid] = None
            return
        view = WolfKillView(self, uid, targets)
        e = discord.Embed(title="🐺 狼人行动",
                          description=f"第 {self.day} 夜，请选择今晚要刀的人：",
                          color=discord.Color.dark_red())
        msg = await send_dm(uid, embed=e, view=view)
        if msg is None:
            self.wolf_votes[uid] = None
            return
        view.message = msg
        await view.wait()
        if not view.chosen:
            self.wolf_votes[uid] = None

    async def ask_seer(self, uid):
        targets = [u for u in self.players if self.alive[u] and u != uid]
        if not targets:
            return
        view = SeerCheckView(self, uid, targets)
        e = discord.Embed(title="🔮 预言家行动",
                          description=f"第 {self.day} 夜，请选择要查验的人：",
                          color=discord.Color.blue())
        msg = await send_dm(uid, embed=e, view=view)
        if msg is None:
            return
        view.message = msg
        await view.wait()

    async def ask_witch(self, uid):
        killed = self.night_kill
        if not self.witch_heal_used and killed and killed != uid:
            view = WitchHealView(self, uid, killed)
            e = discord.Embed(
                title="🧪 女巫  解药",
                description=f"今晚 {self.num(killed)}号 {self.player_name(killed)} 被狼人刀了。\n"
                            f"你要用解药救他吗？",
                color=discord.Color.purple())
            msg = await send_dm(uid, embed=e, view=view)
            if msg:
                view.message = msg
                await view.wait()
        if not self.witch_poison_used:
            targets = [u for u in self.players if self.alive[u] and u != uid]
            if targets:
                view = WitchPoisonView(self, uid, targets)
                e = discord.Embed(title="🧪 女巫  毒药",
                                  description="你要用毒药毒死一个人吗？",
                                  color=discord.Color.dark_purple())
                msg = await send_dm(uid, embed=e, view=view)
                if msg:
                    view.message = msg
                    await view.wait()

    # ---------- 白天 ----------
    async def day_phase(self):
        if self.last_deaths:
            names = "、".join(f"{self.num(u)}号 {self.player_name(u)}" for u in self.last_deaths)
            await self.channel.send(f"🌞 天亮了！昨晚死亡的有：{names}")
        else:
            await self.channel.send("🌞 天亮了！昨晚是平安夜，无人死亡。")
        await asyncio.sleep(2)

        await self.channel.send(
            f"🎤 现在进入轮流发言环节，每人 {WOLF_SPEAK_TIME} 秒，"
            f"可以点「结束发言」提前跳过。")
        await asyncio.sleep(1)

        for u in self.players:
            if self.alive[u]:
                await self.speak_one(u)

        await self.vote_phase()

    async def speak_one(self, uid):
        view = WolfSpeakView(self, uid)
        msg = await self.channel.send(
            f"🎤 请 {self.num(uid)}号 {self.player_name(uid)} 发言"
            f"（{WOLF_SPEAK_TIME} 秒）", view=view)
        view.message = msg
        await view.wait()
        if not view.finished:
            try:
                await msg.edit(
                    content=f"⏰ {self.num(uid)}号 {self.player_name(uid)} 发言时间到。",
                    view=None)
            except Exception:
                pass

    async def vote_phase(self):
        self.votes = {}
        self.vote_event = asyncio.Event()
        self.vote_open = True

        view = WolfVoteEntryView(self)
        msg = await self.channel.send(
            f"🗳️ 投票开始（{WOLF_VOTE_TIME} 秒）\n"
            f"点击下方按钮投票，票数最多的人出局。", view=view)
        view.message = msg

        try:
            await asyncio.wait_for(self.vote_event.wait(), timeout=WOLF_VOTE_TIME)
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass

        self.vote_open = False

        for c in view.children:
            c.disabled = True
        try:
            await msg.edit(view=view)
        except Exception:
            pass

        if not self.votes:
            await self.channel.send("🗳️ 没有人投票，本轮无人出局。")
            return

        counts = {}
        for voter, target in self.votes.items():
            counts[target] = counts.get(target, 0) + 1

        lines = [f"{self.num(t)}号 {self.player_name(t)}：{c} 票"
                 for t, c in sorted(counts.items(), key=lambda x: -x[1])]
        await self.channel.send("🗳️ 投票结果：\n" + "\n".join(lines))
        await asyncio.sleep(1)

        top = max(counts.values())
        candidates = [t for t, c in counts.items() if c == top]
        if len(candidates) > 1:
            out = random.choice(candidates)
            await self.channel.send(
                f"⚖️ 平票！随机决定：{self.num(out)}号 {self.player_name(out)} 出局。")
        else:
            out = candidates[0]

        await self.eliminate(out)

    async def eliminate(self, uid):
        if self.roles[uid] == "白痴" and uid not in self.idiot_revealed:
            self.idiot_revealed.add(uid)
            self.no_vote.add(uid)
            await self.channel.send(
                f"🃏 {self.num(uid)}号 {self.player_name(uid)} 翻牌——"
                f"他是**白痴**！\n他活了下来，但从此失去投票权。")
            return
        self.alive[uid] = False
        await self.channel.send(
            f"💀 {self.num(uid)}号 {self.player_name(uid)} 被投票出局。")

    # ---------- 结算 ----------
    async def announce_result(self):
        lines = []
        for u in self.players:
            r = self.roles[u]
            st = "存活" if self.alive[u] else "出局"
            lines.append(f"{self.num(u)}号 {self.player_name(u)}：{r}  {st}")
        await self.channel.send(
            f"🏁 游戏结束！**{self.winner}** 获胜！\n\n" + "\n".join(lines)
            + "\n\n本局游戏已结束~想再玩就再点一次 /游戏")

# ==============================================================
#                     五 子 棋 · 基 础
# ==============================================================
def cols_for(size):
    return ALL_COLS[:size]


def render_board(board, size):
    cols = cols_for(size)
    lines = ["    " + " ".join(cols)]
    for i in range(size):
        row = []
        for j in range(size):
            v = board[i][j]
            row.append("●" if v == 1 else ("○" if v == 2 else "·"))
        lines.append(f"{i+1:>2}  " + " ".join(row))
    return "```\n" + "\n".join(lines) + "\n```"


def parse_coord(text, size):
    t = text.strip().upper()
    t = re.sub(r"^(落子|下棋|下子|走棋|走|下|放|落)\s*", "", t).strip()
    if not t:
        return None
    cols = cols_for(size)
    m = re.match(r"^([A-Z])\s*(\d{1,2})$", t)
    if m and m.group(1) in cols:
        col = ord(m.group(1)) - 65
        row = int(m.group(2)) - 1
        if 0 <= row < size and 0 <= col < size:
            return row, col
        return None
    m = re.match(r"^(\d{1,2})\s*([A-Z])$", t)
    if m and m.group(2) in cols:
        row = int(m.group(1)) - 1
        col = ord(m.group(2)) - 65
        if 0 <= row < size and 0 <= col < size:
            return row, col
        return None
    return None


_DIRS = ((0, 1), (1, 0), (1, 1), (1, -1))


def check_win(board, r, c, player, size):
    for dr, dc in _DIRS:
        count = 1
        for sign in (1, -1):
            for k in range(1, 5):
                rr, cc = r + dr * k * sign, c + dc * k * sign
                if 0 <= rr < size and 0 <= cc < size and board[rr][cc] == player:
                    count += 1
                else:
                    break
        if count >= 5:
            return True
    return False


# ==============================================================
#                     五 子 棋 · A I
# ==============================================================
_PATTERNS = [
    ("XXXXX",      10_000_000),
    (".XXXX.",     500_000),
    ("XXXX.",      60_000),
    (".XXXX",      60_000),
    ("XXX.X",      55_000),
    ("X.XXX",      55_000),
    ("XX.XX",      55_000),
    (".XXX.",      30_000),
    (".X.XX.",     25_000),
    (".XX.X.",     25_000),
    ("XXX..",      1_200),
    ("..XXX",      1_200),
    ("X.XX.",      1_200),
    (".XX.X",      1_200),
    (".XX.",       800),
    (".X.X.",      700),
    ("XX..",       60),
    ("..XX",       60),
    (".X.X",       60),
    ("X.X.",       60),
    (".X.",        15),
]
_PATTERNS.sort(key=lambda x: -x[1])


def _line_str(board, r, c, dr, dc, me, size):
    chars = []
    for k in range(-4, 5):
        if k == 0:
            chars.append("X")
            continue
        rr, cc = r + dr * k, c + dc * k
        if not (0 <= rr < size and 0 <= cc < size):
            chars.append("O")
        else:
            v = board[rr][cc]
            if v == me:
                chars.append("X")
            elif v == 0:
                chars.append(".")
            else:
                chars.append("O")
    return "".join(chars)


def _line_score(s):
    for pat, sc in _PATTERNS:
        if pat in s:
            return sc
    return 0


def evaluate(board, r, c, me, size):
    return sum(_line_score(_line_str(board, r, c, dr, dc, me, size))
               for dr, dc in _DIRS)


def _has_neighbor(board, r, c, size, dist=2):
    for dr in range(-dist, dist + 1):
        for dc in range(-dist, dist + 1):
            if dr == 0 and dc == 0:
                continue
            rr, cc = r + dr, c + dc
            if 0 <= rr < size and 0 <= cc < size and board[rr][cc] != 0:
                return True
    return False


def ai_move(board, me, size, difficulty="hard"):
    opp = 3 - me
    if not any(board[r][c] for r in range(size) for c in range(size)):
        return size // 2, size // 2
    cands = [(r, c)
             for r in range(size) for c in range(size)
             if board[r][c] == 0 and _has_neighbor(board, r, c, size)]
    if not cands:
        for r in range(size):
            for c in range(size):
                if board[r][c] == 0:
                    return r, c
    scored = []
    for r, c in cands:
        my = evaluate(board, r, c, me, size)
        op = evaluate(board, r, c, opp, size)
        scored.append((my + op * 0.9, my, op, r, c))
    scored.sort(key=lambda x: -x[0])
    for s in scored:
        if s[1] >= 10_000_000:
            return s[3], s[4]
    block_prob = 0.85 if difficulty == "easy" else 1.0
    for s in scored[:6]:
        if s[2] >= 10_000_000:
            if random.random() < block_prob:
                return s[3], s[4]
            break
    if difficulty == "easy":
        pool = scored[:12] if len(scored) >= 12 else scored
        pick = random.choice(pool)
        return pick[3], pick[4]
    if difficulty == "hard":
        for s in scored:
            if s[1] >= 500_000:
                return s[3], s[4]
        for s in scored:
            if s[2] >= 500_000:
                return s[3], s[4]
        for s in scored:
            if s[1] >= 30_000:
                return s[3], s[4]
        for s in scored:
            if s[2] >= 30_000:
                return s[3], s[4]
        return scored[0][3], scored[0][4]
    top_score = scored[0][0]
    pool = [s for s in scored[:8] if s[0] >= top_score * 0.7][:4]
    pick = random.choice(pool or [scored[0]])
    return pick[3], pick[4]


# ==============================================================
#                     五 子 棋 · 对 局
# ==============================================================
class GomokuGame:
    def __init__(self, black_id, white_id, size=15,
                 white_is_bot=False, difficulty="hard", bot_name=None):
        self.size = size
        self.board = [[0] * size for _ in range(size)]
        self.black_id = black_id
        self.white_id = white_id
        self.white_is_bot = white_is_bot
        self.difficulty = difficulty
        self.bot_name = bot_name
        self.turn = 1
        self.history = []
        self.winner = None
        self.board_msg = None
        self.moves = 0
        self.queue = None

    def place(self, r, c):
        self.board[r][c] = self.turn
        self.history.append((r, c, self.turn))
        self.moves += 1
        return check_win(self.board, r, c, self.turn, self.size)

    def undo(self, steps=2):
        removed = 0
        for _ in range(steps):
            if not self.history:
                break
            r, c, _ = self.history.pop()
            self.board[r][c] = 0
            self.moves -= 1
            removed += 1
        return removed > 0

    def coord_str(self, r, c):
        return f"{cols_for(self.size)[c]}{r+1}"

    def status_text(self):
        if self.white_is_bot:
            diff_name = {"easy": "简单", "normal": "一般", "hard": "很难"}[self.difficulty]
            p1 = f"<@{self.black_id}>  ● 黑棋（先手）"
            p2 = f"**{self.bot_name}**  ○ 白棋（后手 · {diff_name}）"
        else:
            p1 = f"<@{self.black_id}>  ● 黑棋（先手）"
            p2 = f"<@{self.white_id}>  ○ 白棋（后手）"
        return f"{p1}\n{p2}"

    def current_player_id(self):
        return self.black_id if self.turn == 1 else self.white_id

    def current_player_mention(self):
        if self.turn == 2 and self.white_is_bot:
            return f"**{self.bot_name}**"
        return f"<@{self.current_player_id()}>"


# ==============================================================
#              五 子 棋 · 坐 标 下 拉
# ==============================================================
class CoordPickerView(discord.ui.View):
    def __init__(self, game, player_id):
        super().__init__(timeout=180)
        self.game = game
        self.player_id = player_id
        self.sel_col = None
        self.sel_row = None
        cols = cols_for(game.size)
        col_options = [discord.SelectOption(label=c, value=c) for c in cols]
        row_options = [discord.SelectOption(label=f"第 {i+1} 行", value=str(i))
                       for i in range(game.size)]
        col_select = discord.ui.Select(placeholder="① 先选列", options=col_options, row=0)
        row_select = discord.ui.Select(placeholder="② 再选行", options=row_options, row=1)
        col_select.callback = self._on_col
        row_select.callback = self._on_row
        self.add_item(col_select)
        self.add_item(row_select)

    async def _on_col(self, interaction):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("汪！这不是你的～", ephemeral=True)
            return
        self.sel_col = interaction.data["values"][0]
        if self.sel_row is not None:
            await self._try_place(interaction)
        else:
            await interaction.response.defer()

    async def _on_row(self, interaction):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("汪！这不是你的～", ephemeral=True)
            return
        self.sel_row = interaction.data["values"][0]
        if self.sel_col is not None:
            await self._try_place(interaction)
        else:
            await interaction.response.defer()

    async def _try_place(self, interaction):
        col = ord(self.sel_col) - 65
        row = int(self.sel_row)
        coord_str = f"{self.sel_col}{row + 1}"
        if self.game.board[row][col] != 0:
            await interaction.response.send_message("汪！那儿已经有子了～", ephemeral=True)
            return
        await interaction.response.edit_message(content=f"🐾 下好啦：**{coord_str}**", view=None)
        if self.game.queue is not None:
            await self.game.queue.put(("place", (row, col)))


# ==============================================================
#              五 子 棋 · 棋 盘 面 板
# ==============================================================
class GamePanel(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game

    @discord.ui.button(label="下子", emoji="🐾", style=discord.ButtonStyle.primary)
    async def place(self, interaction, button):
        if interaction.user.id != self.game.current_player_id():
            await interaction.response.send_message("汪！还没到你～", ephemeral=True)
            return
        await interaction.response.send_message(
            "🐾 先选列，再选行，就落好啦～",
            view=CoordPickerView(self.game, interaction.user.id),
            ephemeral=True)

    @discord.ui.button(label="悔棋", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def undo(self, interaction, button):
        if interaction.user.id != self.game.current_player_id():
            await interaction.response.send_message("汪！还没到你～", ephemeral=True)
            return
        await interaction.response.send_message("汪！收到～", ephemeral=True)
        if self.game.queue is not None:
            await self.game.queue.put(("undo", None))

    @discord.ui.button(label="认输", emoji="🏳️", style=discord.ButtonStyle.danger)
    async def resign(self, interaction, button):
        if interaction.user.id != self.game.current_player_id():
            await interaction.response.send_message("汪！还没到你～", ephemeral=True)
            return
        await interaction.response.send_message("汪！收到～", ephemeral=True)
        if self.game.queue is not None:
            await self.game.queue.put(("resign", None))


class UndoRequestView(discord.ui.View):
    def __init__(self, requester_id, opponent_id):
        super().__init__(timeout=30)
        self.requester_id = requester_id
        self.opponent_id = opponent_id
        self.result = None

    @discord.ui.button(label="汪！可以悔", style=discord.ButtonStyle.success)
    async def agree(self, interaction, button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("汪！这不是问你的啦～", ephemeral=True)
            return
        self.result = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="汪！不许悔", style=discord.ButtonStyle.secondary)
    async def refuse(self, interaction, button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("汪！这不是问你的啦～", ephemeral=True)
            return
        self.result = False
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True


class InviteView(discord.ui.View):
    def __init__(self, challenger_id, target_id, size):
        super().__init__(timeout=60)
        self.challenger_id = challenger_id
        self.target_id = target_id
        self.size = size

    @discord.ui.button(label="汪！来啦", style=discord.ButtonStyle.success)
    async def accept(self, interaction, button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("汪！这不是叫你的啦～", ephemeral=True)
            return
        if interaction.channel.id in _games:
            await interaction.response.send_message("汪！频道里已经有局在下了～", ephemeral=True)
            return
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content=f"汪！<@{self.target_id}> 点头啦，{self.size}×{self.size} 开下～",
            view=self)
        self.stop()
        game = GomokuGame(self.challenger_id, self.target_id, size=self.size)
        _games[interaction.channel.id] = game
        asyncio.create_task(run_game(game, interaction.channel))

    @discord.ui.button(label="汪…不要", style=discord.ButtonStyle.secondary)
    async def refuse(self, interaction, button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("汪！这不是叫你的啦～", ephemeral=True)
            return
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content=f"汪…<@{self.target_id}> 拒绝啦，下次再约。", view=self)
        self.stop()


class BoardSizeView(discord.ui.View):
    def __init__(self, player_id, bot_name, difficulty):
        super().__init__(timeout=120)
        self.player_id = player_id
        self.bot_name = bot_name
        self.difficulty = difficulty

    async def _start(self, interaction, size):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("汪！这不是你的～", ephemeral=True)
            return
        if interaction.channel.id in _games:
            await interaction.response.send_message("汪！频道里已经有局在下了～", ephemeral=True)
            return
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content=f"汪！{size}×{size} 棋盘，开下啦～", view=self)
        game = GomokuGame(self.player_id, None, size=size,
                          white_is_bot=True,
                          difficulty=self.difficulty, bot_name=self.bot_name)
        _games[interaction.channel.id] = game
        asyncio.create_task(run_game(game, interaction.channel))

    @discord.ui.button(label="小 9×9", style=discord.ButtonStyle.success)
    async def small(self, interaction, button):
        await self._start(interaction, 9)

    @discord.ui.button(label="中 13×13", style=discord.ButtonStyle.primary)
    async def medium(self, interaction, button):
        await self._start(interaction, 13)

    @discord.ui.button(label="大 15×15", style=discord.ButtonStyle.danger)
    async def large(self, interaction, button):
        await self._start(interaction, 15)


class GomokuOpponentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="🐱 和月月下（简单）", style=discord.ButtonStyle.success)
    async def month(self, interaction, button):
        await interaction.response.edit_message(
            content="🐾 和月月下～先选棋盘大小：",
            view=BoardSizeView(interaction.user.id, "月月", "easy"))

    @discord.ui.button(label="🦊 和起起下（很难）", style=discord.ButtonStyle.danger)
    async def qiqi(self, interaction, button):
        await interaction.response.edit_message(
            content="🐾 和起起下～先选棋盘大小：",
            view=BoardSizeView(interaction.user.id, "起起", "hard"))

    @discord.ui.button(label="👥 和真人下", style=discord.ButtonStyle.primary)
    async def human(self, interaction, button):
        await interaction.response.send_message(
            "汪！在频道里发 `@对方 小棋盘/中棋盘/大棋盘` 就能约战啦～",
            ephemeral=True)


# ==============================================================
#              五 子 棋 · 刷 新 与 主 循 环
# ==============================================================
async def refresh_board(game):
    body = render_board(game.board, game.size)
    head = f"🐾 **五子棋 · {game.size}×{game.size}**\n{game.status_text()}\n"
    current = game.current_player_mention()
    color = "● 黑棋" if game.turn == 1 else "○ 白棋"
    text = (head
            + f"轮到 {current} 啦（{color}）\n"
            + "\n" + body
            + "\n发 `H8` 或 `8H` 直接下｜点下面按钮也行")
    try:
        await game.board_msg.edit(content=text, view=GamePanel(game))
    except discord.NotFound:
        pass
    except discord.HTTPException as e:
        print(f"[五子棋] 编辑棋盘失败：{e}")


async def run_game(game, channel):
    game.queue = asyncio.Queue()

    async def message_listener():
        while game.winner is None:
            try:
                msg = await bot.wait_for(
                    "message",
                    check=lambda m: m.channel.id == channel.id and not m.author.bot,
                )
            except asyncio.CancelledError:
                return
            except Exception:
                return
            current = game.current_player_id()
            if current is None or msg.author.id != current:
                continue
            txt = msg.content.strip()
            handled = False
            if txt in ("认输", "投降"):
                await game.queue.put(("resign", None))
                handled = True
            elif txt == "悔棋":
                await game.queue.put(("undo", None))
                handled = True
            else:
                mv = parse_coord(txt, game.size)
                if mv:
                    await game.queue.put(("place", mv))
                    handled = True
            if handled:
                try:
                    await msg.delete()
                except Exception:
                    pass

    listener = asyncio.create_task(message_listener())

    try:
        text = (f"🐾 **五子棋 · {game.size}×{game.size}**\n"
                f"{game.status_text()}\n\n"
                f"{render_board(game.board, game.size)}\n"
                f"发 `H8` 或 `8H` 下子｜点下面按钮也行🐾")
        game.board_msg = await channel.send(content=text, view=GamePanel(game))

        while game.winner is None:
            if game.turn == 2 and game.white_is_bot:
                await refresh_board(game)
                await asyncio.sleep(1.0)
                r, c = ai_move(game.board, game.turn, game.size, game.difficulty)
                win = game.place(r, c)
                if win:
                    game.winner = game.turn
                    await refresh_board(game)
                    await channel.send(f"🎉 汪！**{game.bot_name}** 五子连珠，赢啦～")
                    break
                if game.moves >= game.size * game.size:
                    game.winner = 0
                    await refresh_board(game)
                    await channel.send("🐾 下满了，平局～")
                    break
                game.turn = 3 - game.turn
                continue

            await refresh_board(game)
            current_id = game.current_player_id()

            while not game.queue.empty():
                try:
                    game.queue.get_nowait()
                except Exception:
                    break

            # 无限等待，不再有超时
            action, data = await game.queue.get()

            if action == "resign":
                winner_turn = 3 - game.turn
                game.winner = winner_turn
                wt = (f"**{game.bot_name}**" if winner_turn == 2 and game.white_is_bot
                      else f"<@{game.black_id if winner_turn == 1 else game.white_id}>")
                await refresh_board(game)
                await channel.send(f"🏳️ 汪！<@{current_id}> 认输啦。🎉 {wt} 赢啦")
                break

            if action == "undo":
                if len(game.history) == 0:
                    await channel.send("汪！还没下呢，悔什么啦～")
                    continue
                if game.white_is_bot:
                    game.undo(2)
                    game.turn = 1
                    await refresh_board(game)
                    continue
                else:
                    opp_id = (game.white_id if current_id == game.black_id
                              else game.black_id)
                    view = UndoRequestView(current_id, opp_id)
                    ask = await channel.send(
                        f"汪！<@{current_id}> 想悔棋，<@{opp_id}> 点头吗？",
                        view=view)
                    await view.wait()
                    try:
                        await ask.delete()
                    except Exception:
                        pass
                    if view.result is True:
                        game.undo(2)
                        game.turn = 1 if current_id == game.black_id else 2
                    await refresh_board(game)
                    continue

            if action == "place":
                r, c = data
                if game.board[r][c] != 0:
                    await channel.send(f"🐾 <@{current_id}> 那儿已经有子啦，换一个位置～")
                    continue
                win = game.place(r, c)
                if win:
                    game.winner = game.turn
                    await refresh_board(game)
                    await channel.send(f"🎉 汪！<@{current_id}> 五子连珠，赢啦！")
                    break
                if game.moves >= game.size * game.size:
                    game.winner = 0
                    await refresh_board(game)
                    await channel.send("🐾 下满了，平局～")
                    break
                game.turn = 3 - game.turn

    except Exception as e:
        print(f"[五子棋] 对局异常：{e}")
        try:
            await channel.send(f"汪…出错了，这局算啦：{e}")
        except Exception:
            pass
    finally:
        listener.cancel()
        try:
            await listener
        except Exception:
            pass
        _games.pop(channel.id, None)

# ==============================================================
#                   骗 子 酒 馆 · 模 块
#        只在频道 1557161825521631322 里才能开
# ==============================================================

# ---------- 牌名 ----------
CARD_CAT = "🐱猫牌"
CARD_FOX = "🦊狐牌"
CARD_DOG = "🐶狗牌"
CARD_JOKER = "⭐万能牌"

DECK_COMPOSITION = {
    CARD_CAT: 6,
    CARD_FOX: 6,
    CARD_DOG: 6,
    CARD_JOKER: 2,
}
TARGET_POOL = [CARD_CAT, CARD_FOX, CARD_DOG]
CARD_ORDER = [CARD_CAT, CARD_FOX, CARD_DOG, CARD_JOKER]


def get_hand_size(num_players):
    # 无论几人，统一每人 5 张
    return 5


def make_liar_deck(num_players):
    """初始牌库。3 人局加 1 张万能牌。"""
    deck = []
    for card, n in DECK_COMPOSITION.items():
        deck.extend([card] * n)
    if num_players == 3:
        deck.append(CARD_JOKER)
    random.shuffle(deck)
    return deck


BOT_PROFILES = {
    "躺躺": {"uid": 900000000000000003, "difficulty": "easy",
             "trust": "汪……信你", "challenge": "汪！怀疑你！"},
    "起起": {"uid": 900000000000000002, "difficulty": "medium",
             "trust": "喵～信你一回", "challenge": "喵！起起质疑！"},
    "月月": {"uid": 900000000000000001, "difficulty": "hard",
             "trust": "呜～信你一次", "challenge": "呜！月月觉得你在骗人！"},
}


def hand_to_str(hand):
    if not hand:
        return "（没牌了）"
    from collections import Counter
    cnt = Counter(hand)
    lines = []
    for c in CARD_ORDER:
        if cnt.get(c):
            lines.append(f"{c}x{cnt[c]}")
    return "\n".join(lines)


def is_truthful(cards, target):
    return all(c == target or c == CARD_JOKER for c in cards)


class LiarGame:
    def __init__(self, channel_id, host_id):
        self.channel_id = channel_id
        self.host_id = host_id
        self.players = [host_id]
        self.alive = {host_id: True}
        self.hands = {host_id: []}
        self.roulette_chambers = {host_id: 6}
        self.bot_ids = {}
        self.deck = []
        self.discard = []
        self.target_rank = None
        self.current_idx = 0
        self.round = 0
        self.queue = None
        self.lobby_msg = None
        self.is_bot_mode = False
        self.last_play = None
        self.hand_size = 5
        self.version = 0   # 每次状态变化 +1，用来让旧面板失效

    def add_player(self, uid, bot_name=None):
        if uid in self.players:
            return
        self.players.append(uid)
        self.alive[uid] = True
        self.hands[uid] = []
        self.roulette_chambers[uid] = 6
        if bot_name:
            self.bot_ids[uid] = bot_name

    def alive_players(self):
        return [p for p in self.players if self.alive.get(p)]

    def current_player(self):
        return self.players[self.current_idx] if self.players else None

    def next_alive(self):
        n = len(self.players)
        for _ in range(n):
            self.current_idx = (self.current_idx + 1) % n
            if self.alive.get(self.players[self.current_idx]):
                return self.players[self.current_idx]
        return None

    def deal(self):
        """共享牌库：把牌库 + 弃牌堆 + 所有手牌收集回来重洗，再按人数发牌。"""
        alive = self.alive_players()
        n = len(alive)
        if n <= 0:
            return

        pool = list(self.deck) + list(self.discard)
        for p in self.players:
            pool.extend(self.hands.get(p, []))
            self.hands[p] = []

        if not pool:
            pool = make_liar_deck(n)

        random.shuffle(pool)

        # ★ 动态数值：目标每人发 get_hand_size(n) 张，但不超过牌库能平分的上限
        target = get_hand_size(n)
        max_per = len(pool) // n
        self.hand_size = max(1, min(target, max_per))

        self.deck = pool
        self.discard = []

        for _ in range(self.hand_size):
            for p in alive:
                if self.deck:
                    self.hands[p].append(self.deck.pop())

    def new_round(self):
        self.round += 1
        self.target_rank = random.choice(TARGET_POOL)

    def check_winner(self):
        alive = self.alive_players()
        return alive[0] if len(alive) == 1 else None

    def name_of(self, uid):
        if uid in self.bot_ids:
            return f"**{self.bot_ids[uid]}**"
        return f"<@{uid}>"

    def roulette_shot_at(self, shooter_uid):
        ch = self.roulette_chambers.get(shooter_uid, 6)
        if ch <= 0:
            ch = 1
        hit = random.randint(1, ch) == 1
        if hit:
            self.roulette_chambers[shooter_uid] = 6
        else:
            self.roulette_chambers[shooter_uid] = ch - 1
        return hit, self.roulette_chambers.get(shooter_uid, 6)

    def bump_version(self):
        """状态变化 → 旧面板作废"""
        self.version += 1


# ---------- 机器人逻辑（不显示给玩家）----------
def bot_pick_cards(hand, target, bot_name):
    if not hand:
        return []
    real_idx = [i for i, c in enumerate(hand)
                if c == target or c == CARD_JOKER]
    fake_idx = [i for i in range(len(hand)) if i not in real_idx]

    if bot_name == "躺躺":
        if real_idx:
            k = random.randint(1, min(len(real_idx), 3))
            return random.sample(real_idx, k)
        if fake_idx:
            k = random.randint(1, min(len(fake_idx), 3))
            return random.sample(fake_idx, k)
        return []

    if bot_name == "起起":
        want_lie = random.random() < 0.5
        if want_lie and fake_idx:
            k = random.randint(1, min(len(fake_idx), 3))
            return random.sample(fake_idx, k)
        if real_idx:
            k = random.randint(1, min(len(real_idx), 2))
            return random.sample(real_idx, k)
        if fake_idx:
            k = random.randint(1, min(len(fake_idx), 3))
            return random.sample(fake_idx, k)
        return []

    # 月月
    if real_idx and (len(real_idx) >= 2 or random.random() < 0.7):
        k = random.randint(1, min(len(real_idx), 3))
        return random.sample(real_idx, k)
    if fake_idx:
        k = random.randint(1, min(len(fake_idx), 3))
        return random.sample(fake_idx, k)
    if real_idx:
        return random.sample(real_idx, 1)
    return []


def bot_decide_challenge(hand, target, claimed_qty, bot_name):
    if bot_name == "躺躺":
        return random.random() < 0.15

    my_hand = hand or []
    my_targets = sum(1 for c in my_hand if c == target)
    my_jokers = sum(1 for c in my_hand if c == CARD_JOKER)
    total_target_deck = DECK_COMPOSITION.get(target, 6)
    total_joker_deck = DECK_COMPOSITION.get(CARD_JOKER, 2)
    max_remaining = max(0, (total_target_deck - my_targets)
                        + (total_joker_deck - my_jokers))
    over_claim = claimed_qty > max_remaining

    if bot_name == "起起":
        if over_claim:
            return True
        return random.random() < 0.45

    # 月月
    if over_claim:
        return True
    if max_remaining > 0 and claimed_qty > max_remaining * 0.7:
        return random.random() < 0.6
    return random.random() < 0.25


# ---------- 大厅 ----------
class LiarLobbyView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=180)
        self.game = game

    @discord.ui.button(label="加入", emoji="🐾", style=discord.ButtonStyle.primary)
    async def join(self, interaction, button):
        uid = interaction.user.id
        if uid in self.game.players:
            await interaction.response.send_message("你已经加入啦～", ephemeral=True)
            return
        if self.game.is_bot_mode:
            await interaction.response.send_message("机器人模式不让别人加进来～", ephemeral=True)
            return
        if len(self.game.players) >= LIAR_MAX_PLAYERS:
            await interaction.response.send_message("房间满啦～", ephemeral=True)
            return
        self.game.add_player(uid)
        await interaction.response.send_message("汪！加入成功～", ephemeral=True)
        await self._update()

    @discord.ui.button(label="机器人模式", emoji="🤖", style=discord.ButtonStyle.secondary)
    async def bot_mode(self, interaction, button):
        if interaction.user.id != self.game.host_id:
            await interaction.response.send_message("只有房主能用～", ephemeral=True)
            return
        if len(self.game.players) > 1:
            await interaction.response.send_message("已经有人加入了，不能混着玩～", ephemeral=True)
            return
        await interaction.response.send_message(
            "汪！选一个模式吧～",
            view=LiarBotModeView(self.game), ephemeral=True)

    @discord.ui.button(label="开始", emoji="▶️", style=discord.ButtonStyle.success)
    async def start(self, interaction, button):
        if interaction.user.id != self.game.host_id:
            await interaction.response.send_message("只有房主能开始哦～", ephemeral=True)
            return
        if len(self.game.players) < 2:
            await interaction.response.send_message("至少 2 人才能开始～", ephemeral=True)
            return
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        asyncio.create_task(run_liar_game(self.game, interaction.channel))

    async def _update(self):
        try:
            embed = discord.Embed(
                title="🎭 骗子酒馆 · 房间",
                description=(
                    f"房主：<@{self.game.host_id}>\n"
                    f"玩家（{len(self.game.players)}/{LIAR_MAX_PLAYERS}）：\n"
                    + "\n".join(f"· {self.game.name_of(p)}" for p in self.game.players)
                    + "\n\n点「加入」进来，房主点「开始」～\n"
                    + "不懂规则？输入「规则」显示。"
                ),
                color=discord.Color.dark_gold())
            await self.game.lobby_msg.edit(embed=embed, view=self)
        except Exception:
            pass


class LiarBotModeView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=60)
        self.game = game

    @discord.ui.button(label="单人模式", emoji="👤", style=discord.ButtonStyle.primary)
    async def solo(self, interaction, button):
        if interaction.user.id != self.game.host_id:
            await interaction.response.send_message("只有房主能用～", ephemeral=True)
            return
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content="汪！选一个对手：", view=LiarBotPickView(self.game))

    @discord.ui.button(label="多人模式", emoji="👥", style=discord.ButtonStyle.success)
    async def multi(self, interaction, button):
        if interaction.user.id != self.game.host_id:
            await interaction.response.send_message("只有房主能用～", ephemeral=True)
            return
        if len(self.game.players) > 1:
            await interaction.response.send_message("房间已经有别的人了～", ephemeral=True)
            return
        self.game.is_bot_mode = True
        for name in ["躺躺", "起起", "月月"]:
            self.game.add_player(BOT_PROFILES[name]["uid"], name)
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content="汪！躺躺、起起、月月都坐下啦～开始发牌！", view=None)
        self.stop()
        try:
            await self.game.lobby_msg.edit(view=None)
        except Exception:
            pass
        await interaction.channel.send(
            f"🎭 汪！<@{self.game.host_id}> 和躺躺、起起、月月 开一局骗子酒馆～")
        asyncio.create_task(run_liar_game(self.game, interaction.channel))


class LiarBotPickView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=60)
        self.game = game

    async def _start_bot(self, interaction, bot_name):
        if interaction.user.id != self.game.host_id:
            await interaction.response.send_message("只有房主能用～", ephemeral=True)
            return
        if len(self.game.players) > 1:
            await interaction.response.send_message("房间已经有别的人了～", ephemeral=True)
            return
        profile = BOT_PROFILES[bot_name]
        self.game.is_bot_mode = True
        self.game.add_player(profile["uid"], bot_name)
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content=f"汪！{bot_name} 坐下啦～开始发牌！", view=None)
        self.stop()
        try:
            await self.game.lobby_msg.edit(view=None)
        except Exception:
            pass
        await interaction.channel.send(
            f"🎭 汪！<@{self.game.host_id}> 和 **{bot_name}** 开一局骗子酒馆～")
        asyncio.create_task(run_liar_game(self.game, interaction.channel))

    @discord.ui.button(label="🐶 躺躺（简单）", style=discord.ButtonStyle.success)
    async def tangtang(self, interaction, button):
        await self._start_bot(interaction, "躺躺")

    @discord.ui.button(label="🐱 起起（中等）", style=discord.ButtonStyle.primary)
    async def qiqi(self, interaction, button):
        await self._start_bot(interaction, "起起")

    @discord.ui.button(label="🐙 月月（困难）", style=discord.ButtonStyle.danger)
    async def month(self, interaction, button):
        await self._start_bot(interaction, "月月")


# ---------- 出牌 UI（按牌序排列 + version 防过期） ----------
class LiarPlayView(discord.ui.View):
    def __init__(self, game, uid, version):
        super().__init__(timeout=LIAR_TIMEOUT + 30)
        self.game = game
        self.uid = uid
        self.version = version
        self.sel_indices = []
        self._confirmed = False

        hand = game.hands.get(uid, [])
        # 按 CARD_ORDER 分组，同类的排在一起
        ordered = []
        for c in CARD_ORDER:
            for i, card in enumerate(hand):
                if card == c:
                    ordered.append((i, card))

        options = []
        for display_idx, (orig_idx, card) in enumerate(ordered):
            options.append(discord.SelectOption(
                label=f"{display_idx+1}. {card}",
                value=str(orig_idx)))
        if options:
            select = discord.ui.Select(
                placeholder="选择你要出的牌（最多 3 张，可改）",
                options=options,
                min_values=1,
                max_values=min(3, len(options)),   # ← 限制最多 3 张
                row=0)
            select.callback = self._on_select
            self.add_item(select)

        confirm = discord.ui.Button(
            label="确认出牌", style=discord.ButtonStyle.success, row=1)
        confirm.callback = self._on_confirm
        self.add_item(confirm)

    async def _on_select(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("这不是你的回合～", ephemeral=True)
            return
        if self.version != self.game.version:
            await interaction.response.send_message(
                "这轮已经过去了，这个面板过期啦～", ephemeral=True)
            return
        self.sel_indices = [int(v) for v in interaction.data["values"]]
        await interaction.response.defer()

    async def _on_confirm(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("这不是你的回合～", ephemeral=True)
            return
        if self.version != self.game.version:
            await interaction.response.send_message(
                "这轮已经过去了，这个面板过期啦～", ephemeral=True)
            return
        if self._confirmed:
            return
        if not self.sel_indices:
            await interaction.response.send_message("汪！先选几张牌～", ephemeral=True)
            return
        hand = self.game.hands.get(self.uid, [])
        idxs = sorted(i for i in self.sel_indices if 0 <= i < len(hand))
        if not idxs:
            await interaction.response.send_message("汪！没选到牌～", ephemeral=True)
            return
        if len(idxs) > 3:                      # ← 兜底：最多 3 张
            await interaction.response.send_message(
                "汪！一次最多出 3 张牌哦～", ephemeral=True)
            return
        self._confirmed = True

        await interaction.response.edit_message(content="✅ 出牌已确认", view=None)
        if self.game.queue is not None:
            await self.game.queue.put(("play", (self.uid, idxs)))


class LiarChallengeView(discord.ui.View):
    def __init__(self, game, chooser_id):
        super().__init__(timeout=LIAR_CHALLENGE_TIMEOUT)
        self.game = game
        self.chooser_id = chooser_id

    @discord.ui.button(label="相信", emoji="🤝", style=discord.ButtonStyle.success)
    async def trust(self, interaction, button):
        if interaction.user.id != self.chooser_id:
            await interaction.response.send_message("还没到你～", ephemeral=True)
            return
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        if self.game.queue is not None:
            await self.game.queue.put(("trust", None))

    @discord.ui.button(label="质疑", emoji="🔍", style=discord.ButtonStyle.danger)
    async def challenge(self, interaction, button):
        if interaction.user.id != self.chooser_id:
            await interaction.response.send_message("还没到你～", ephemeral=True)
            return
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        if self.game.queue is not None:
            await self.game.queue.put(("challenge", None))

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.game.queue is not None:
            await self.game.queue.put(("trust", None))


class LiarPlayBtnView(discord.ui.View):
    def __init__(self, game, uid, version):
        super().__init__(timeout=LIAR_TIMEOUT + 10)
        self.game = game
        self.uid = uid
        self.version = version

    @discord.ui.button(label="看手牌 / 出牌", emoji="🎴",
                       style=discord.ButtonStyle.primary)
    async def open_hand(self, interaction, button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("这不是你的回合～", ephemeral=True)
            return
        if self.version != self.game.version:
            await interaction.response.send_message(
                "这轮已经过去了，这个按钮过期啦～", ephemeral=True)
            return
        hand = self.game.hands.get(self.uid, [])
        if not hand:
            await interaction.response.send_message("你没牌了～", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**{LIAR_TIMEOUT} 秒时间，超时随机挑手牌出**\n"
            f"本轮需出：**{self.game.target_rank}**\n\n"
            f"**你的手牌：**\n{hand_to_str(hand)}\n\n"
            f"选你要出的牌（可多选、可改），点「确认出牌」～",
            view=LiarPlayView(self.game, self.uid, self.version),
            ephemeral=True)


# ---------- 质疑判定 ----------
async def liar_handle_challenge(game, channel, chooser, play_owner):
    await channel.send(f"🔍 {game.name_of(chooser)} 质疑！翻开看看…")
    await asyncio.sleep(1.2)

    declared_rank = game.last_play["declared_rank"]
    revealed = game.last_play["cards"]
    reveal_str = "、".join(f"`{c}`" for c in revealed)

    if game.last_play["is_truth"]:
        shooter = play_owner
        target = chooser
        await channel.send(
            f"😲 翻开：{reveal_str}\n"
            f"全是 `{declared_rank}`（或万能牌），声明属实！\n"
            f"{game.name_of(chooser)} 质疑错了——"
            f"{game.name_of(play_owner)} 要开枪打 {game.name_of(chooser)}！")
    else:
        shooter = play_owner
        target = play_owner
        await channel.send(
            f"😱 翻开：{reveal_str}\n"
            f"谎报成 `{declared_rank}`！\n"
            f"{game.name_of(play_owner)} 撒谎被抓，要用自己的点数枪自己…")

    await asyncio.sleep(1.5)
    chambers = game.roulette_chambers.get(shooter, 6)
    await channel.send(
        f"🔫 {game.name_of(shooter)} 用自己的左轮"
        f"（当前弹膛：1/{chambers}）对准 {game.name_of(target)}…")
    await asyncio.sleep(2)
    hit, left = game.roulette_shot_at(shooter)
    if hit:
        game.alive[target] = False
        who = game.name_of(target)

        # 隐藏死法：3% 概率
        if random.random() < 0.03:
            await channel.send(
                f"🎆 **砰——呲啦——！**\n"
                f"射向 {who} 的枪膛里装的不是子弹，是一整管烟花弹。\n"
                f"火线一烧到底，从后脑勺炸开——脑浆、彩带、金色纸屑，"
                f"还有几朵没来得及绽放的礼花，一起开在了天花板上，"
                f"红红绿绿，像一场盛大的告别。\n"
                f"很漂亮。也很安静。\n"
                f"**{who} 出局。**"
            )
        else:
            DEATH_LINES = [
                "💥 **砰！** {who} 慢慢倒了下去，像一袋被丢下的土豆。血从太阳穴流下来，在地板上积成一小滩。没人说话。",
                "💥 **砰！** {who} 的嘴角渗出一丝血，顺着下巴滴下去。他张了张嘴，没发出声音，就靠着墙滑坐下去，眼睛还睁着。",
                "💥 **砰！** {who} 忽然就累了。所有的力气从身体里被抽走，膝盖先着地，然后是肩，最后脸颊贴在那冰凉的地板上。血还在流，人已经安静了。",
                "💥 **砰！** {who} 站了一瞬，像要反驳什么，然后向后倒下去。他的手指还在动，像在数什么——也许是数还剩几秒。",
                "💥 **砰！** {who} 的头向后一仰，很轻，轻得像打了个盹。血从后脑流过脖颈，把领子染深了。屋里的人都听见那声闷响，然后什么都没听见。",
                "💥 **砰！** {who} 跪了下去，像要祈祷。没有人回应。血沿着脸颊流下来，滴在手背上，一滴，两滴。他一直跪着，没再起来。",
                "💥 **砰！** {who} 的手还搭在桌边，人已经坐倒在地。嘴角挂着血，不知道是不是笑。周围那么静，静得能听见血滴在地板上的声音。",
                "💥 **砰！** {who} 睁着眼，一直看着天花板，好像那儿有什么好看的东西。血从太阳穴慢慢淌出来，在地板上画出一小片影子。他再没动过。",
                "💥 **砰！** {who} 慢慢地倒下去，像这一切都已经不重要了。血顺着嘴角滑下来，把衣领染红。他的呼吸越来越浅，越来越浅，然后停了。",
                "💥 **砰！** {who} 像被谁轻轻推了一下，向后踉跄两步，靠在了墙上。血从额角流下来，遮住半只眼睛。他低下头，像在数地上的木纹，然后头就再没抬起来。",
            ]
            line = random.choice(DEATH_LINES).format(who=who)
            await channel.send(line + f"\n**{who} 出局！**")
    else:
        await channel.send(
            f"💨 咔哒…空膛。{game.name_of(target)} 活下来了～\n"
            f"（{game.name_of(shooter)} 的弹膛剩 1/{left}）")

async def run_liar_game(game, channel):
    game.queue = asyncio.Queue()
    game.deal()
    game.round = 0
    game.last_play = None

    try:
        n = len(game.players)
        deck_info = "、".join(f"`{c}×{n2}`" for c, n2 in DECK_COMPOSITION.items())
        extra = "（+1 张万能牌）" if n == 3 else ""
        await channel.send(
            f"📦 **本局牌库**：{deck_info}{extra}\n"
            f"共 **{n}** 人，每人发 **{game.hand_size} 张**，顺序发牌。\n"
            f"🎭 一轮目标固定。一个人出牌后，其他所有人依次选择相信/质疑，"
            f"**都相信**才轮到下一个人出牌。")

        game.new_round()
        game.bump_version()
        await channel.send(
            f"\n🎴 **第 {game.round} 轮**　目标牌：**`{game.target_rank}`**")

        safety = 0
        while True:
            safety += 1
            if safety > 3000:
                await channel.send("⚠️ 异常，游戏结束。")
                break

            winner = game.check_winner()
            if winner:
                await channel.send(f"🏆 汪！{game.name_of(winner)} 是最后的赢家～")
                break

            alive_players = game.alive_players()
            if not any(game.hands.get(p) for p in alive_players):
                await channel.send("🎴 手牌全出光，本轮结束！重新发牌～")
                game.deal()
                game.new_round()
                game.last_play = None
                game.bump_version()
                await channel.send(
                    f"\n🎴 **第 {game.round} 轮**　目标牌：**`{game.target_rank}`**")
                continue

            current = game.current_player()
            if current is None or not game.alive.get(current):
                game.next_alive()
                continue

                 # ---------- 跳到有手牌的玩家 ----------
            skip_safety = 0
            while not game.hands.get(current):
                skip_safety += 1
                if skip_safety > 20:
                    break
                nxt = game.next_alive()
                if nxt is None:
                    break
                current = nxt

            # 所有活人都没牌 → 本轮结束，重发牌
            if not game.hands.get(current):
                await channel.send("🎴 所有人都出光手牌，本轮结束——重新发牌～")
                game.deal()
                game.new_round()
                game.last_play = None
                game.bump_version()
                await channel.send(
                    f"\n🎴 **第 {game.round} 轮**　目标牌：**`{game.target_rank}`**")
                continue

            # ---------- 只剩一个有手牌的人？ → 强制质疑上一手 ----------
            alive_with_cards = [p for p in game.alive_players()
                                if game.hands.get(p)]
            if len(alive_with_cards) == 1 and current == alive_with_cards[0]:
                if game.last_play is not None and game.last_play["player"] != current:
                    prev = game.last_play["player"]
                    await channel.send(
                        f"⚠️ **只剩 {game.name_of(current)} 还有手牌，"
                        f"没有下家能质疑他——强制质疑上一手"
                        f"（{game.name_of(prev)} 出的）！**")
                    await liar_handle_challenge(game, channel, current, prev)
                    game.bump_version()
                    winner = game.check_winner()
                    if winner:
                        await channel.send(f"🏆 汪！{game.name_of(winner)} 是最后的赢家～")
                        break
                    game.deal()
                    game.new_round()
                    game.last_play = None
                    game.bump_version()
                    await channel.send(
                        f"\n🎴 **第 {game.round} 轮**　目标牌：**`{game.target_rank}`**")
                    continue
                else:
                    await channel.send(
                        "🎴 只剩一个人有手牌，上一手也是自己出的——本轮结束，重发牌～")
                    game.deal()
                    game.new_round()
                    game.last_play = None
                    game.bump_version()
                    await channel.send(
                        f"\n🎴 **第 {game.round} 轮**　目标牌：**`{game.target_rank}`**")
                    continue

            # ---------- 出牌 ----------
            is_bot = current in game.bot_ids
            bot_name = game.bot_ids.get(current)
            hand = game.hands.get(current, [])
            my_version = game.version

            await channel.send(
                f"🎴 轮到 {game.name_of(current)} 了～"
                f"（手上 {len(hand)} 张牌）")

            if is_bot:
                await asyncio.sleep(1.0)
                idxs = bot_pick_cards(hand, game.target_rank, bot_name)
                if not idxs:
                    idxs = [0]
                await channel.send(f"💭 {game.name_of(current)} 思考了一下…")
            else:
                await channel.send(
                    f"{game.name_of(current)} 点下面按钮看手牌、出牌～\n"
                    f"⏱️ **{LIAR_TIMEOUT} 秒**内，超时随机挑手牌出",
                    view=LiarPlayBtnView(game, current, my_version))

                while not game.queue.empty():
                    try:
                        game.queue.get_nowait()
                    except Exception:
                        break

                try:
                    action, data = await asyncio.wait_for(
                        game.queue.get(), timeout=LIAR_TIMEOUT + 30)
                except asyncio.TimeoutError:
                    await channel.send(
                        f"⏰ {game.name_of(current)} 超时，随机挑牌出～")
                    idxs = random.sample(range(len(hand)),
                                         random.randint(1, min(3, len(hand))))
                else:
                    if action == "play":
                        _, idxs = data
                    else:
                        continue

            idxs = sorted(set(i for i in idxs if 0 <= i < len(hand)))
            if not idxs:
                game.next_alive()
                continue

            played = []
            for i in sorted(idxs, reverse=True):
                if 0 <= i < len(game.hands[current]):
                    card = game.hands[current].pop(i)
                    game.discard.append(card)
                    played.append(card)
            played = played[::-1]

            n_cards = len(played)
            truthful = is_truthful(played, game.target_rank)

            game.bump_version()

            await channel.send(
                f"🎴 {game.name_of(current)} 宣称："
                f"**「我出 {n_cards} 张 {game.target_rank}」**"
                f"（手里还剩 {len(game.hands[current])} 张）")

            game.last_play = {
                "player": current,
                "cards": played,
                "declared_rank": game.target_rank,
                "declared_qty": n_cards,
                "is_truth": truthful,
            }

            # ---------- 其他所有玩家依次质疑 ----------
            start_idx = game.players.index(current)
            voters_ordered = []
            for i in range(1, len(game.players) + 1):
                p = game.players[(start_idx + i) % len(game.players)]
                if p != current and game.alive.get(p):
                    voters_ordered.append(p)

            anyone_challenged = False
            challenger = None
            for voter in voters_ordered:
                voter_is_bot = voter in game.bot_ids
                voter_name = game.bot_ids.get(voter)

                if voter_is_bot:
                    await asyncio.sleep(0.8)
                    chall = bot_decide_challenge(
                        game.hands.get(voter, []),
                        game.target_rank, n_cards, voter_name)
                    decision = "challenge" if chall else "trust"
                    profile = BOT_PROFILES[voter_name]
                    line = profile["challenge"] if chall else profile["trust"]
                    await channel.send(f"{game.name_of(voter)}：{line}")
                else:
                    await channel.send(
                        f"⏳ {game.name_of(voter)}，{LIAR_CHALLENGE_TIMEOUT} 秒内"
                        f"点「相信」或「质疑」，超时默认相信～",
                        view=LiarChallengeView(game, voter))
                    while not game.queue.empty():
                        try:
                            game.queue.get_nowait()
                        except Exception:
                            break
                    try:
                        decision, _ = await asyncio.wait_for(
                            game.queue.get(), timeout=LIAR_CHALLENGE_TIMEOUT + 2)
                    except asyncio.TimeoutError:
                        decision = "trust"
                    if decision not in ("trust", "challenge"):
                        decision = "trust"

                if decision == "challenge":
                    anyone_challenged = True
                    challenger = voter
                    break
                else:
                    await channel.send(f"🤝 {game.name_of(voter)} 相信了～")

            if anyone_challenged:
                await liar_handle_challenge(game, channel, challenger, current)
                game.bump_version()
                winner = game.check_winner()
                if winner:
                    await channel.send(f"🏆 汪！{game.name_of(winner)} 是最后的赢家～")
                    break
                game.deal()
                game.new_round()
                game.last_play = None
                game.bump_version()
                await channel.send(
                    f"\n🎴 **第 {game.round} 轮**　目标牌：**`{game.target_rank}`**")
                continue

            # 所有人都相信 → 轮下家出牌
            game.current_idx = start_idx
            next_p = game.next_alive()
            if next_p is None:
                game.deal()
                game.new_round()
                game.last_play = None
                game.bump_version()
                await channel.send(
                    f"\n🎴 **第 {game.round} 轮**　目标牌：**`{game.target_rank}`**")
                continue
            continue

    except Exception as e:
        print(f"[骗子酒馆] 异常：{e}")
        try:
            await channel.send(f"汪…出错了：{e}")
        except Exception:
            pass
    finally:
        _liar_games.pop(channel.id, None)

# ---------- 规则命令 ----------
@bot.listen("on_message")
async def liar_rule_listener(message):
    if message.author.bot or not message.guild:
        return
    try:
        txt = message.content.strip()
        if txt not in ("规则", "酒馆规则", "骗子酒馆规则", "规则说明"):
            return
        if message.channel.id not in LIAR_CHANNEL_IDS:
            return
        embed = discord.Embed(
            title="🎭 骗子酒馆 · 规则",
            description=(
                "**牌库（共 20 张，3 人局 +1 万能牌 = 21 张）**\n"
                "🐱 `猫牌` × 6\n"
                "🦊 `狐牌` × 6\n"
                "🐶 `狗牌` × 6\n"
                "⭐ `万能牌` × 2 ← 万能，可以当任何牌\n\n"
                "**开局**\n"
                "· 无论人数几何，每人发 **5 张**\n\n"
                "**共享牌库**\n"
                "所有牌都在同一副牌库中，每一轮结束重洗剩下所有牌，"
                "**总数永远守恒**。谁手上能有多少张，你都能算出来。\n\n"
                "**一轮的流程**\n"
                "1. 系统随机指定一个「目标牌」（猫牌 / 狐牌 / 狗牌）\n"
                "2. **一轮中目标牌不变**，大家轮流出牌\n"
                "3. 每次出牌：轮到的人声称「我出 N 张 目标牌」，实际可以出别的牌\n"
                "4. 出完后，下家 20 秒内选择相信或质疑（超时默认相信）\n"
                "5. **相信** → 下家接着出牌\n"
                "6. **质疑** → 翻牌：\n"
                "   · 全是目标牌或万能牌 → **出牌人**用**自己的左轮点数**"
                "打**质疑者**\n"
                "   · 有谎报 → 出牌人用**自己的左轮点数**打**自己**\n\n"
                "**一轮结束的条件**\n"
                "· 有人质疑 → 判罚后本轮结束，全部人重发牌\n"
                "· 手牌全出光 → 本轮结束，全部人重发牌\n"
                "· 轮到一个手上没牌的人出牌（无人接牌） → **他直接出局**\n\n"
                "**俄罗斯轮盘**\n"
                "6 个弹膛、1 颗子弹：\n"
                "第 1 次 1/6 → 活下来 1/5 → 1/4 → 1/3 → 1/2 → 1/1（必死）\n"
                "中弹的人出局，弹膛重置。\n\n"
                "**胜利**：活到最后的玩家赢。\n\n"
                "**三位对手**\n"
                "🐶 **躺躺**（简单）— 老老实实，基本相信\n"
                "🐱 **起起**（中等）— 会数牌，爱质疑\n"
                "🐙 **月月**（困难）— 数牌精准，出牌很稳"
            ),
            color=discord.Color.dark_gold())
        await message.channel.send(embed=embed)
    except Exception as e:
        print(f"[骗子酒馆·规则] 异常：{e}")

# ---------- 五子棋约战监听 ----------
@bot.listen("on_message")
async def gomoku_invite_listener(message):
    if message.author.bot or not message.guild:
        return
    if message.channel.id in _games:
        return
    content = message.content.strip()
    m = re.match(r"^<@!?(\d+)>\s*(小棋盘|中棋盘|大棋盘|小|中|大|五子棋)$", content)
    if not m:
        return
    target_id = int(m.group(1))
    kw = m.group(2)
    size_map = {"小": 9, "中": 13, "大": 15,
                "小棋盘": 9, "中棋盘": 13, "大棋盘": 15,
                "五子棋": 15}
    size = size_map[kw]
    if target_id == message.author.id:
        await message.channel.send("汪！不能和自己下啦～")
        return
    if bot.user and target_id == bot.user.id:
        await message.channel.send("汪！想和这边的下？用 /游戏 按钮选月月或起起～")
        return
    try:
        target = message.guild.get_member(target_id)
        if target is None:
            target = await message.guild.fetch_member(target_id)
    except discord.NotFound:
        await message.channel.send("汪…找不到这个人。")
        return
    except discord.HTTPException as e:
        await message.channel.send(f"汪…找人出错了：{e}")
        return
    await message.channel.send(
        f"🐾 汪！<@{message.author.id}> 邀 <@{target_id}> 下 **{size}×{size}** 五子棋～\n"
        f"<@{target_id}>，60 秒内点「汪！来啦」就开下🐾",
        view=InviteView(message.author.id, target_id, size))

# ================= 游戏面板（斜杠指令入口） =================
class GamePanelView(discord.ui.View):
    """游戏面板：点一个就在当前频道发起报名"""

    def __init__(self):
        super().__init__(timeout=180)

    async def _start_join(self, interaction, label, view):
        ch = interaction.channel
        busy = game_busy(ch.id)
        if busy:
            await interaction.response.send_message(
                f"这个频道已经有一局「{busy}」在进行中了，等这局结束再来~", ephemeral=True)
            return
        game_lock(ch.id, label)
        view.channel_ref = ch
        view.channel_id = ch.id  # 关键：确保超时/失败时能解锁
        try:
            await interaction.response.send_message(content=view.content_text(), view=view)
            view.message = await interaction.original_response()
        except Exception:
            game_unlock(ch.id)
            raise

    @discord.ui.button(label="🎭 真心话大冒险", style=discord.ButtonStyle.success, custom_id="game_panel_td")
    async def td_btn(self, interaction, button):
        await self._start_join(interaction, "真心话大冒险", TruthDareJoinView(interaction.user.id))

    @discord.ui.button(label="🐺 狼人杀", style=discord.ButtonStyle.primary, custom_id="game_panel_wolf")
    async def wolf_btn(self, interaction, button):
        await self._start_join(interaction, "狼人杀", WolfJoinView(interaction.user.id))


# ==============================================================
#              统一游戏菜单（五子棋 / 骗子酒馆 / 真心话 / 狼人杀）
# ==============================================================
class MainGameMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="五子棋", emoji="⚫", style=discord.ButtonStyle.primary, row=0)
    async def gomoku(self, interaction, button):
        await interaction.response.edit_message(
            content="🐾 五子棋 · 选个对手吧：",
            embed=None,
            view=GomokuOpponentView())

    @discord.ui.button(label="骗子酒馆", emoji="🎭", style=discord.ButtonStyle.danger, row=0)
    async def liar(self, interaction, button):
        cid = interaction.channel_id
        gid = interaction.guild_id
        
        # 获取当前服务器对应的酒馆频道ID
        allowed_channel_id = LIAR_CHANNEL_CONFIG.get(gid)
        
        # 如果当前服务器没有配置酒馆，或者当前不在对应的酒馆频道中
        if not allowed_channel_id or cid != allowed_channel_id:
            if allowed_channel_id:
                # 只导向当前服务器对应的那个频道
                await interaction.response.send_message(
                    f"汪！这个游戏只能在游戏区的专属频道玩哦 → <#{allowed_channel_id}>",
                    ephemeral=True)
            else:
                # 没有配置酒馆频道的服务器给出的兜底提示
                await interaction.response.send_message(
                    "汪！当前服务器还没有配置骗子酒馆的专属频道哦~",
                    ephemeral=True)
            return

        # ===== 以下代码保持不变 =====
        if cid in _liar_games:
            await interaction.response.send_message(
                "这个频道已经有酒馆房间了～", ephemeral=True)
            return

        if cid in _liar_games:
            await interaction.response.send_message(
                "这个频道已经有酒馆房间了～", ephemeral=True)
            return
        game = LiarGame(cid, interaction.user.id)
        _liar_games[cid] = game
        view = LiarLobbyView(game)
        embed = discord.Embed(
            title="🎭 骗子酒馆",
            description=(
                f"房主：<@{interaction.user.id}>\n"
                f"玩家（1/{LIAR_MAX_PLAYERS}）：\n· <@{interaction.user.id}>"
                f"\n\n看不懂？不会玩？输入[规则]查看详细。\n"
                f"\n\n点「加入」进来，房主点「开始」～\n"
                f"（房主也可以点 🤖 机器人模式 单人测试）"
            ),
            color=discord.Color.dark_gold())
        await interaction.response.edit_message(content=None, embed=embed, view=view)
        game.lobby_msg = await interaction.original_response()

    @discord.ui.button(label="真心话大冒险", emoji="🎲", style=discord.ButtonStyle.success, row=1)
    async def td(self, interaction, button):
        ch = interaction.channel
        busy = game_busy(ch.id)
        if busy:
            await interaction.response.send_message(
                f"这个频道已经有一局「{busy}」在进行中了，等这局结束再来~",
                ephemeral=True)
            return
        game_lock(ch.id, "真心话大冒险")
        view = TruthDareJoinView(interaction.user.id)
        view.channel_ref = ch
        view.channel_id = ch.id
        await interaction.response.edit_message(
            content=view.content_text(), embed=None, view=view)
        view.message = await interaction.original_response()

    @discord.ui.button(label="狼人杀", emoji="🐺", style=discord.ButtonStyle.primary, row=1)
    async def wolf(self, interaction, button):
        ch = interaction.channel
        busy = game_busy(ch.id)
        if busy:
            await interaction.response.send_message(
                f"这个频道已经有一局「{busy}」在进行中了，等这局结束再来~",
                ephemeral=True)
            return
        game_lock(ch.id, "狼人杀")
        view = WolfJoinView(interaction.user.id)
        view.channel_ref = ch
        view.channel_id = ch.id
        await interaction.response.edit_message(
            content=view.content_text(), embed=None, view=view)
        view.message = await interaction.original_response()


@bot.tree.command(name="游戏", description="汪！来玩游戏吧")
async def slash_game(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🐾 汪！要玩什么？",
        description=(
            "⚫ **五子棋** — 月月 / 起起 / 真人\n"
            "🎭 **骗子酒馆** — 2~4 人诈唬对决（仅限专属频道）\n"
            "🎲 **真心话大冒险** — 掷骰子比大小（2~8 人）\n"
            "🐺 **狼人杀** — 6 人局，身份私信发放"
        ),
        color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, view=MainGameMenuView())

# ================= 消息监听 =================
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content_raw = message.content.strip()

    # ========================================================
    # 1) 预设指路 —— 任何频道只要出现关键词就会发链接
    # ========================================================
    if not message.author.bot and any(k in content_raw for k in PRESET_KEYWORDS):
        await message.channel.send(
            f"📖 指路这里 👇\n"
            f"{PRESET_LINK}\n"
            f"**kemini 奶人缝合教学**"
        )
        await bot.process_commands(message)
        return

    # ========================================================
    # 2) 投喂彩蛋 —— 任何频道都能触发
    # ========================================================
    if "投喂" in content_raw and "给月月" in content_raw and not message.author.bot:
        food_match = content_raw.replace("投喂", "").replace("给月月", "").strip()
        if food_match:
            feed_responses = [
                f"呜哇！{message.author.mention} 给本座带了{food_match}！超开心！🦊💕",
                f"汪！{message.author.mention} 专门投喂{food_match}！触手感动打结了！🐙✨",
                f"嗷呜~ {message.author.mention} 的{food_match}好好吃！🦊🐙"
            ]
            await message.channel.send(random.choice(feed_responses))
            await bot.process_commands(message)
            return

    # ========================================================
    # 3) @月月 的自动回复 —— 只在 MENTION_CHANNEL_ID 这个频道触发
    # ========================================================
    if bot.user.mentioned_in(message) and message.channel.id == MENTION_CHANNEL_ID:
        content = message.content.lower()
        author_id = message.author.id

        # 奶茶梗
        if "我要买奶茶" in content or "我要喝奶茶" in content:
            milk_tea_waiting[author_id] = True
            await message.channel.send(
                f"汪汪！{message.author.mention} 你要买奶茶吗？本座用触手给你摇！🧋🐙")
            await bot.process_commands(message)
            return

        if milk_tea_waiting.get(author_id) and "qqㄋㄟㄋㄟ好喝到咩噗茶" in content:
            del milk_tea_waiting[author_id]
            await message.channel.send(
                f"QQㄋㄟㄋㄟ好喝到咩噗茶？！{message.author.mention} "
                f"你居然真的点了！本座给你做！偷偷加一勺小鱼干！🐙🦊🧋")
            await bot.process_commands(message)
            return

        # 梗彩蛋
        for _keys, _replies in MEME_EGGS:
            if any(_k in content for _k in _keys):
                await message.channel.send(
                    f"{message.author.mention} {random.choice(_replies)}")
                await bot.process_commands(message)
                return

        # 状态提醒
        if bot_data["thirst"] < 30 and random.random() < 0.6:
            await message.channel.send(
                f"呜呜... {message.author.mention} 本座好渴... 能喝点水吗？🥺💧")
            await bot.process_commands(message)
            return
        if bot_data["hunger"] < 30 and random.random() < 0.6:
            await message.channel.send(
                f"呜呜... {message.author.mention} 本座肚子饿了... 喂点好吃的？🥺")
            await bot.process_commands(message)
            return
        if bot_data["energy"] < 20 and random.random() < 0.6:
            await message.channel.send(
                f"哈欠... {message.author.mention} 本座好困... 除非有咖啡... 😴☕")
            await bot.process_commands(message)
            return

        # 亲昵
        if any(word in content for word in ["亲亲", "抱抱", "举高高", "贴贴",
                                             "喜欢你", "爱你", "摸摸", "rua"]):
            bot_data["affection"] = min(520, bot_data["affection"] + 2)
            await message.channel.send(
                f"呜哇！{message.author.mention} 亲亲！狐狸耳朵红透了！🦊🐙💕 *(亲密度 +2)*")
            await bot.process_commands(message)
            return

        # 骂人
        if any(word in content for word in ["弱智", "笨蛋", "傻子", "蠢",
                                             "丑", "滚", "讨厌"]):
            bot_data["affection"] = max(0, bot_data["affection"] - 5)
            await message.channel.send(
                f"呜哇！{message.author.mention} 你骂本座！本座去角落画圈圈！😭 *(亲密度 -5)*")
            await bot.process_commands(message)
            return

        # 萌新指路
        if "子区" in content or "右上角" in content:
            await message.channel.send(
                f"汪汪！{message.author.mention} 子区在**右上角**哦！🐙👉")
            await bot.process_commands(message)
            return
        elif "标注" in content:
            await message.channel.send(
                f"嗷呜~ {message.author.mention} 发图时长按图片添加标注！🦊✨")
            await bot.process_commands(message)
            return
        elif "频道" in content or "在哪" in content:
            await message.channel.send(
                f"呜哇！{message.author.mention} 频道在左侧列表！🐙💦")
            await bot.process_commands(message)
            return
        elif "发图" in content or "发照片" in content:
            await message.channel.send(
                f"汪！{message.author.mention} 拖图片进来或点➕号！🦊📸")
            await bot.process_commands(message)
            return
        elif "@" in content or "艾特" in content or "at人" in content:
            await message.channel.send(
                f"嗷呜~ {message.author.mention} 打 @ 选名字！🐙")
            await bot.process_commands(message)
            return
        elif "审核" in content:
            await message.channel.send(
                f"萌新宝宝！{message.author.mention} 去 **#在这里审核** 看看！🦊🔐")
            await bot.process_commands(message)
            return
        elif "规则" in content:
            await message.channel.send(
                f"汪汪！{message.author.mention} 先看 **#欢迎-和-规则**！🐙😭")
            await bot.process_commands(message)
            return
        elif "萌新" in content or "不会玩" in content or "新人" in content:
            await message.channel.send(
                f"诶？{message.author.mention} 不会玩？问 **#💬聊聊**！本座罩着你！🦊💕")
            await bot.process_commands(message)
            return

        # 酒馆指路
        elif "单人卡" in content:
            await message.channel.send(
                f"汪汪！{message.author.mention} 单人卡去 **#单人卡**！🦊🃏")
            await bot.process_commands(message)
            return
        elif "群像卡" in content or "群像" in content:
            await message.channel.send(
                f"嗷呜~ {message.author.mention} 群像卡去 **#群像卡**！🐙✨")
            await bot.process_commands(message)
            return
        elif "章鱼特供" in content:
            await message.channel.send(
                f"汪！{message.author.mention} 章鱼特供在 **#章鱼特供**！🦊🐙")
            await bot.process_commands(message)
            return
        elif "美化" in content or "组件" in content:
            await message.channel.send(
                f"嗷呜~ {message.author.mention} 去 **#美化和小组件**！🐙✨")
            await bot.process_commands(message)
            return
        elif "点菜" in content:
            await message.channel.send(
                f"汪！{message.author.mention} 去 **#📝点菜**！🦊🍽️")
            await bot.process_commands(message)
            return
        elif "番外" in content:
            await message.channel.send(
                f"嗷呜！{message.author.mention} 番外去 **#吃点番外**！🐙📖")
            await bot.process_commands(message)
            return
        elif "mj" in content or "小铺" in content:
            await message.channel.send(
                f"汪！{message.author.mention} mj去 **#章鱼的mj小铺**！🦊🛍️")
            await bot.process_commands(message)
            return
        elif "bug" in content or "报错" in content:
            await message.channel.send(
                f"呜哇！{message.author.mention} bug看 **#教学-bug看这里**！🐙🔧")
            await bot.process_commands(message)
            return
        elif "绘图" in content or "画画" in content:
            await message.channel.send(
                f"嗷呜！{message.author.mention} 绘图看 **#绘图区规则**！🦊🎨")
            await bot.process_commands(message)
            return
        elif "语音" in content or "音色" in content:
            await message.channel.send(
                f"汪汪！{message.author.mention} 语音去 **#AI语音区**！🐙🎤")
            await bot.process_commands(message)
            return
        elif "金主" in content:
            await message.channel.send(
                f"嘿嘿，{message.author.mention} 金主去 **#感谢金主**！🐙💖")
            await bot.process_commands(message)
            return
        elif "摸鱼" in content:
            await message.channel.send(
                f"汪！{message.author.mention} 本座在 **#摸鱼中**！🦊🐟")
            await bot.process_commands(message)
            return
        elif "拼车" in content or "nai" in content:
            await message.channel.send(
                f"汪！{message.author.mention} 拼车去 **#nai拼车频道**！🐙🚗")
            await bot.process_commands(message)
            return

        # 兜底卖萌
        normal_replies = [
            f"汪汪！{message.author.mention} 你叫我嘛？贴贴！🦊🐙",
            f"嗷呜~ {message.author.mention}！本座在呢！今天也要乖乖的哦！",
            f"{message.author.mention} 嘿嘿，想本座啦？本座刚才玩触手没听到！",
            f"汪！{message.author.mention} 本座听到啦！需要帮忙吗？",
            f"{message.author.mention} 抱抱！触手超软的哦！🦊🐙"
        ]
        await message.channel.send(random.choice(normal_replies))

    await bot.process_commands(message)

if __name__ == "__main__":
    init_db()
    bot.run(os.getenv("DISCORD_TOKEN"))  # 从环境变量读取
