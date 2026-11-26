import os
import re
import json
import secrets
import subprocess
import asyncio
import ipaddress
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

# =====================================================
# CONFIG
# =====================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = "<"
OWNER_ID = 1481235590283530312

MAX_RAM = float(os.getenv("MAX_RAM_GB", "32"))
MAX_CPU = float(os.getenv("MAX_CPU_CORES", "6"))
MAX_DISK = float(os.getenv("MAX_DISK_GB", "80"))

IMAGE = os.getenv("DOCKER_IMAGE", "kts-ubuntu-systemd:24.04")
NETWORK = os.getenv("VPS_NETWORK", "kts-vps-net")
SUBNET = ipaddress.ip_network(
    os.getenv("VPS_SUBNET", "172.30.0.0/16"),
    strict=False
)
GATEWAY = os.getenv("VPS_GATEWAY", str(next(SUBNET.hosts())))

DATA = Path(os.getenv("DATA_DIR", "/opt/kts-vps-bot/data"))
DATA.mkdir(parents=True, exist_ok=True)

DB_FILE = DATA / "vps.json"
ADMINS_FILE = DATA / "admins.json"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None
)

# =====================================================
# DATABASE
# =====================================================

def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2))
    temp.replace(path)


def load_db():
    return read_json(DB_FILE, {})


def save_db(db):
    write_json(DB_FILE, db)


def load_admins():
    return set(read_json(ADMINS_FILE, []))


def save_admins(admins):
    write_json(ADMINS_FILE, sorted(admins))


# =====================================================
# PERMISSIONS
# =====================================================

def is_owner(user):
    return user.id == OWNER_ID


def is_admin(user):
    return is_owner(user) or user.id in load_admins()


def can_manage(user, vps):
    return is_admin(user) or int(vps["owner"]) == user.id


# =====================================================
# DOCKER
# =====================================================

def docker(args, check=True):
    return subprocess.run(
        ["docker", *map(str, args)],
        capture_output=True,
        text=True,
        check=check
    )


def ensure_network():
    result = docker(["network", "inspect", NETWORK], check=False)

    if result.returncode == 0:
        return

    docker([
        "network", "create",
        "--driver", "bridge",
        "--subnet", str(SUBNET),
        "--gateway", GATEWAY,
        NETWORK
    ])


def allocate_ip(db):
    used = {
        item.get("private_ipv4")
        for item in db.values()
        if item.get("private_ipv4")
    }

    for address in SUBNET.hosts():
        ip = str(address)

        if ip != GATEWAY and ip not in used:
            return ip

    raise RuntimeError("No free private IPv4 addresses.")


def container_status(vps):
    result = docker([
        "inspect", "-f", "{{.State.Status}}", vps["name"]
    ], check=False)

    if result.returncode:
        return "missing"

    return result.stdout.strip()


def safe_id():
    return secrets.token_hex(4)


def get_vps(ident):
    return load_db().get(ident)


# =====================================================
# COMPONENTS V2
# =====================================================

def make_container(title, description, components=None, color=0xE95420):
    items = [
        discord.ui.TextDisplay(f"# {title}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(description)
    ]

    if components:
        items.extend([
            discord.ui.Separator(),
            *components
        ])

    return discord.ui.Container(*items, accent_colour=color)


async def reply_v2(ctx, title, description, components=None):
    view = discord.ui.LayoutView(timeout=180)
    view.add_item(make_container(title, description, components))
    await ctx.reply(view=view, mention_author=False)


async def interaction_v2(interaction, title, description, components=None):
    view = discord.ui.LayoutView(timeout=180)
    view.add_item(make_container(title, description, components))

    if interaction.response.is_done():
        await interaction.followup.send(view=view, ephemeral=True)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


# =====================================================
# VPS EXPIRY
# =====================================================

def parse_duration(value):
    value = value.lower().strip()

    if value == "inf":
        return None

    match = re.fullmatch(r"(\d+)(s|m|d|mo|y)", value)
    if not match:
        raise ValueError("Use 30s, 15m, 1d, 1mo, 1y or inf.")

    amount = int(match.group(1))
    unit = match.group(2)

    if amount <= 0:
        raise ValueError("Duration must be greater than zero.")

    seconds = {
        "s": 1,
        "m": 60,
        "d": 86400,
        "mo": 30 * 86400,
        "y": 365 * 86400
    }

    return datetime.now(timezone.utc).timestamp() + amount * seconds[unit]


async def expiry_worker():
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            db = load_db()
            now = datetime.now(timezone.utc).timestamp()

            for ident, vps in list(db.items()):
                expires_at = vps.get("expires_at")

                if expires_at is None or now < expires_at:
                    continue

                print(f"[EXPIRY] Deleting expired VPS {ident}")

                container = docker(
                    ["inspect", vps["name"]],
                    check=False
                )

                if container.returncode == 0:
                    result = docker(
                        ["rm", "-f", vps["name"]],
                        check=False
                    )
                    if result.returncode:
                        print(f"[EXPIRY] Container deletion failed: {result.stderr}")
                        continue

                failed = False

                for key in ("volume", "docker_volume"):
                    volume = vps.get(key)

                    if not volume:
                        continue

                    exists = docker(
                        ["volume", "inspect", volume],
                        check=False
                    )

                    if exists.returncode != 0:
                        continue

                    result = docker(
                        ["volume", "rm", volume],
                        check=False
                    )

                    if result.returncode:
                        print(f"[EXPIRY] Volume deletion failed for {volume}: {result.stderr}")
                        failed = True

                if not failed:
                    db.pop(ident, None)
                    save_db(db)
                    print(f"[EXPIRY] VPS {ident} permanently deleted.")

        except Exception as exc:
            print(f"[EXPIRY] Worker error: {exc}")

        await asyncio.sleep(30)


# =====================================================
# VPS CREATION
# =====================================================

async def create_vps(ctx, member, ram, cpu, disk, duration="inf"):
    try:
        expires_at = parse_duration(duration)
    except ValueError as exc:
        return await reply_v2(ctx, "Invalid expiry timer", str(exc))
    if not is_admin(ctx.author):
        return await reply_v2(
            ctx, "Access denied",
            "Only the bot owner and bot admins can create VPS instances."
        )

    if min(ram, cpu, disk) <= 0:
        return await reply_v2(
            ctx, "Invalid specifications",
            "RAM, CPU and storage must be greater than zero."
        )

    if ram > MAX_RAM or cpu > MAX_CPU or disk > MAX_DISK:
        return await reply_v2(
            ctx,
            "Resource limit exceeded",
            f"Maximum limits:\n"
            f"RAM: **{MAX_RAM:g} GB**\n"
            f"CPU: **{MAX_CPU:g} cores**\n"
            f"Storage request: **{MAX_DISK:g} GB**"
        )

    db = load_db()
    ident = safe_id()

    owned = [v for v in db.values() if int(v.get("owner", 0)) == member.id]
    used = {
        int(v["number"]) for v in owned
        if str(v.get("number", "")).isdigit()
    }

    next_num = 1
    for old_vps in owned:
        if not str(old_vps.get("number", "")).isdigit():
            while next_num in used:
                next_num += 1
            old_vps["number"] = next_num
            used.add(next_num)
            next_num += 1

    vps_number = 1
    while vps_number in used:
        vps_number += 1

    name = f"kts-{member.id}-{vps_number}"
    volume = f"kts-data-{ident}"
    docker_volume = f"kts-docker-{ident}"

    try:
        ensure_network()
        private_ip = allocate_ip(db)

        docker(["volume", "create", volume])
        docker(["volume", "create", docker_volume])

        docker([
            "run", "-d",
            "--name", name,
            "--hostname", name,
            "--restart", "unless-stopped",
            "--network", NETWORK,
            "--ip", private_ip,
            "--privileged",
            "--tmpfs", "/run:rw,nosuid,nodev",
            "--tmpfs", "/run/lock:rw,nosuid,nodev",
            "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw",
            "--memory", f"{ram}g",
            "--memory-swap", f"{ram}g",
            "--cpus", str(cpu),
            "--env", f"KTS_RAM_GIB={ram}",
            "--env", f"KTS_CPU_QUOTA={cpu}",
            "--env", f"KTS_DISK_REQUEST_GIB={disk}",
            "--pids-limit", "1024",
            "-v", f"{volume}:/opt/kts-storage",
            "-v", f"{docker_volume}:/var/lib/docker",
            IMAGE
        ])

        await asyncio.sleep(2)

        state = container_status({"name": name})

        if state != "running":
            logs = docker(["logs", "--tail", "80", name], check=False)
            raise RuntimeError(
                f"Container state: {state}\n{logs.stdout}\n{logs.stderr}"
            )

    except Exception as exc:
        docker(["rm", "-f", name], check=False)
        docker(["volume", "rm", volume], check=False)
        docker(["volume", "rm", docker_volume], check=False)

        return await reply_v2(
            ctx, "Provisioning failed",
            f"```text\n{str(exc)[:1500]}\n```"
        )

    db[ident] = {
        "owner": member.id,
        "number": vps_number,
        "name": name,
        "volume": volume,
        "docker_volume": docker_volume,
        "ram_gib": ram,
        "cpu_quota": cpu,
        "requested_disk_gib": disk,
        "private_ipv4": private_ip,
        "created_by": ctx.author.id,
        "expires_at": expires_at
    }

    save_db(db)

    await reply_v2(
        ctx,
        "VPS created successfully",
        f"**Owner:** {member.mention}\n"
        f"**VPS Number:** `{vps_number}`\n"
        f"**Hostname:** `{name}`\n"
        f"**RAM:** {ram:g} GB\n"
        f"**CPU:** {cpu:g} cores\n"
        f"**Storage requested:** {disk:g} GB\n"
        f"**Private IPv4:** `{private_ip}`\n"
        f"**Expiry:** `{'Unlimited' if expires_at is None else datetime.fromtimestamp(expires_at, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
        "Your VPS is running. Use `<manage` to control it."
    )


@bot.command()
async def create(
    ctx,
    member: discord.Member,
    ram: float,
    cpu: float,
    disk: float,
    duration: str = "inf"
):
    await create_vps(ctx, member, ram, cpu, disk, duration)


# =====================================================
# ADMIN COMMANDS
# =====================================================

@bot.command()
async def admins(ctx):
    admin_ids = load_admins()

    lines = [f"<@{OWNER_ID}> — **Bot Owner**"]
    lines.extend(f"<@{uid}>" for uid in sorted(admin_ids))

    await reply_v2(
        ctx,
        "KTS HOST Administrators",
        "\n".join(lines)
    )


@bot.group(invoke_without_command=True)
async def admin(ctx):
    await reply_v2(
        ctx,
        "Admin management",
        "Commands:\n"
        "`<admin add @user`\n"
        "`<admin remove @user`\n\n"
        "Only the bot owner can manage bot admins."
    )


@admin.command(name="add")
async def admin_add(ctx, member: discord.Member):
    if not is_owner(ctx.author):
        return await reply_v2(
            ctx, "Access denied",
            "Only the bot owner can add bot admins."
        )

    if member.id == OWNER_ID:
        return await reply_v2(
            ctx, "Already owner",
            "The bot owner does not need admin access."
        )

    admin_ids = load_admins()

    if member.id in admin_ids:
        return await reply_v2(
            ctx, "Already an admin",
            f"{member.mention} is already a bot admin."
        )

    admin_ids.add(member.id)
    save_admins(admin_ids)

    await reply_v2(
        ctx, "Admin added",
        f"{member.mention} is now a KTS HOST bot admin."
    )


@admin.command(name="remove")
async def admin_remove(ctx, member: discord.Member):
    if not is_owner(ctx.author):
        return await reply_v2(
            ctx, "Access denied",
            "Only the bot owner can remove bot admins."
        )

    admin_ids = load_admins()

    if member.id not in admin_ids:
        return await reply_v2(
            ctx, "Not an admin",
            f"{member.mention} is not a bot admin."
        )

    admin_ids.remove(member.id)
    save_admins(admin_ids)

    await reply_v2(
        ctx, "Admin removed",
        f"{member.mention} is no longer a bot admin."
    )


# =====================================================
# SSHX
# =====================================================

async def create_sshx_link(vps):
    if container_status(vps) != "running":
        result = docker(["start", vps["name"]], check=False)

        if result.returncode:
            raise RuntimeError(result.stderr)

    script = (
        "mkdir -p /run/kts; "
        "rm -f /run/kts/sshx.log; "
        "nohup sh -c 'script -q -f -c sshx /run/kts/sshx.log' "
        ">/dev/null 2>&1 &"
    )

    result = docker([
        "exec", vps["name"], "bash", "-lc", script
    ], check=False)

    if result.returncode:
        raise RuntimeError(result.stderr)

    for _ in range(40):
        await asyncio.sleep(0.5)

        result = docker([
            "exec", vps["name"],
            "bash", "-lc",
            "cat /run/kts/sshx.log 2>/dev/null || true"
        ], check=False)

        clean = re.sub(
            r"\x1b\[[0-?]*[ -/]*[@-~]",
            "",
            result.stdout
        )

        urls = re.findall(r"https://[^\s<>\"']+", clean)

        if urls:
            return urls[-1].rstrip(".,);]")

    raise RuntimeError("SSHX did not return a link. Check SSHX inside the VPS image.")


# =====================================================
# TARGET RESOLUTION
# =====================================================

def parse_mention(value):
    match = re.fullmatch(r"<@!?(\d+)>", value)

    if match:
        return int(match.group(1))

    return None


def user_vps(user_id):
    db = load_db()

    return [
        (ident, vps)
        for ident, vps in db.items()
        if int(vps["owner"]) == int(user_id)
    ]


def resolve_target(ctx, args, admin_required=False):
    args = list(args)
    target_id = ctx.author.id
    ident = None

    if args:
        mentioned_id = parse_mention(args[0])

        if mentioned_id is not None:
            target_id = mentioned_id
            args.pop(0)

            if target_id != ctx.author.id and not is_admin(ctx.author):
                raise PermissionError("You cannot access another user's VPS.")

    elif is_admin(ctx.author):
        target_id = None

    if args:
        ident = args[0]

    if admin_required and not is_admin(ctx.author):
        raise PermissionError("Only bot admins can use this command.")

    return target_id, ident


# =====================================================
# VPS CONTROL PANEL
# =====================================================

class VPSPanel(discord.ui.LayoutView):
    def __init__(self, ident):
        super().__init__(timeout=180)

        vps = get_vps(ident)

        if not vps:
            raise ValueError("VPS not found.")

        status = container_status(vps)
        display_status = "SUSPENDED" if status == "missing" else status.upper()
        number = vps.get("number", "?")

        details = (
            f"**VPS Number:** `{number}`\n"
            f"**Hostname:** `{vps['name']}`\n"
            f"**Status:** `{display_status}`\n"
            f"**RAM:** {vps['ram_gib']:g} GB\n"
            f"**CPU:** {vps['cpu_quota']:g} Cores\n"
            f"**Storage Requested:** {vps['requested_disk_gib']:g} GB\n"
            f"**Private IP:** `{vps['private_ipv4']}`"
        )

        if status == "missing":
            suspension_message = (
                details + "\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "⚠️ **VPS SUSPENDED — TERMS OF SERVICE VIOLATION**\n\n"
                "Your VPS has been suspended because activity detected "
                "on your server appears to violate KTS HOST's Terms of Service.\n\n"
                "Access to this VPS has been disabled to protect our hosting "
                "infrastructure and maintain service security.\n\n"
                "**What happens now?**\n"
                "- Your VPS is currently inaccessible.\n"
                "- Its resources are unavailable.\n"
                "- Further violations may result in permanent termination.\n\n"
                "📩 **Need assistance?**\n\n"
                "If you believe this suspension was made in error, "
                "please open a support ticket in our Discord server:\n"
                "https://discord.com/channels/1559218178754879640/1559218180704960688"
                "\n\n"
                "📜 **Free Hosting Rules:**\n"
                "https://discord.com/channels/1559218178754879640/1565278001128808538"
                "\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "**KTS HOST** • Fair Usage. Secure Hosting."
            )

            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("# 🚫 KTS HOST • VPS SUSPENDED"),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(suspension_message),
                    accent_colour=0xED4245
                )
            )
            return

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# KTS HOST • VPS Control"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(details),
                discord.ui.Separator(),
                discord.ui.ActionRow(
                    StartButton(ident),
                    StopButton(ident),
                    RestartButton(ident)
                ),
                discord.ui.ActionRow(
                    SSHXButton(ident),
                    DeleteButton(ident)
                ),
                accent_colour=0xE95420
            )
        )


class VPSButton(discord.ui.Button):
    def __init__(self, ident, label, style):
        super().__init__(label=label, style=style)
        self.ident = ident

    async def get_authorized_vps(self, interaction):
        vps = get_vps(self.ident)

        if not vps:
            await interaction.response.send_message(
                "VPS not found.",
                ephemeral=True
            )
            return None

        if not can_manage(interaction.user, vps):
            await interaction.response.send_message(
                "You do not have permission to manage this VPS.",
                ephemeral=True
            )
            return None

        return vps


class StartButton(VPSButton):
    def __init__(self, ident):
        super().__init__(ident, "Start", discord.ButtonStyle.success)

    async def callback(self, interaction):
        vps = await self.get_authorized_vps(interaction)
        if not vps:
            return

        result = docker(["start", vps["name"]], check=False)

        if result.returncode:
            return await interaction.response.send_message(
                f"Start failed: `{result.stderr[:900]}`",
                ephemeral=True
            )

        await interaction.response.send_message(
            "VPS started successfully.",
            ephemeral=True
        )


class StopButton(VPSButton):
    def __init__(self, ident):
        super().__init__(ident, "Stop", discord.ButtonStyle.danger)

    async def callback(self, interaction):
        vps = await self.get_authorized_vps(interaction)
        if not vps:
            return

        result = docker(["stop", vps["name"]], check=False)

        if result.returncode:
            return await interaction.response.send_message(
                f"Stop failed: `{result.stderr[:900]}`",
                ephemeral=True
            )

        await interaction.response.send_message(
            "VPS stopped successfully.",
            ephemeral=True
        )


class RestartButton(VPSButton):
    def __init__(self, ident):
        super().__init__(ident, "Restart", discord.ButtonStyle.primary)

    async def callback(self, interaction):
        vps = await self.get_authorized_vps(interaction)
        if not vps:
            return

        result = docker(["restart", vps["name"]], check=False)

        if result.returncode:
            return await interaction.response.send_message(
                f"Restart failed: `{result.stderr[:900]}`",
                ephemeral=True
            )

        await interaction.response.send_message(
            "VPS restarted successfully.",
            ephemeral=True
        )


class SSHXButton(VPSButton):
    def __init__(self, ident):
        super().__init__(ident, "SSHX Terminal", discord.ButtonStyle.secondary)

    async def callback(self, interaction):
        vps = await self.get_authorized_vps(interaction)
        if not vps:
            return

        await interaction.response.defer(ephemeral=True)

        try:
            url = await create_sshx_link(vps)

            await interaction.followup.send(
                f"**Private SSHX terminal**\n{url}\n\nKeep this link private.",
                ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(
                f"SSHX failed: `{str(exc)[:900]}`",
                ephemeral=True
            )


class DeleteButton(VPSButton):
    def __init__(self, ident):
        super().__init__(ident, "Delete VPS", discord.ButtonStyle.danger)

    async def callback(self, interaction):
        vps = await self.get_authorized_vps(interaction)
        if not vps:
            return

        if not is_admin(interaction.user):
            return await interaction.response.send_message(
                "Only bot admins can delete VPS instances.",
                ephemeral=True
            )

        await interaction.response.send_message(
            view=DeleteConfirmView(self.ident),
            ephemeral=True
        )


class DeleteConfirmView(discord.ui.LayoutView):
    def __init__(self, ident):
        super().__init__(timeout=60)

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Confirm VPS deletion"),
                discord.ui.TextDisplay(
                    "This permanently deletes the VPS container and its attached data volumes."
                ),
                discord.ui.ActionRow(
                    ConfirmDeleteButton(ident),
                    CancelDeleteButton()
                ),
                accent_colour=0xED4245
            )
        )


class ConfirmDeleteButton(discord.ui.Button):
    def __init__(self, ident):
        super().__init__(
            label="Confirm Delete",
            style=discord.ButtonStyle.danger
        )
        self.ident = ident

    async def callback(self, interaction):
        if not is_admin(interaction.user):
            return await interaction.response.send_message(
                "Only bot admins can delete VPS instances.",
                ephemeral=True
            )

        vps = get_vps(self.ident)

        if not vps:
            return await interaction.response.send_message(
                "VPS not found.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        result = docker(["rm", "-f", vps["name"]], check=False)

        if result.returncode:
            return await interaction.followup.send(
                f"Container deletion failed: `{result.stderr[:900]}`",
                ephemeral=True
            )

        errors = []

        for key in ("volume", "docker_volume"):
            volume = vps.get(key)

            if volume:
                result = docker(["volume", "rm", volume], check=False)

                if result.returncode:
                    errors.append(f"{volume}: {result.stderr.strip()}")

        db = load_db()
        db.pop(self.ident, None)
        save_db(db)

        if errors:
            return await interaction.followup.send(
                "Container deleted, but some volumes could not be removed:\n"
                + "\n".join(errors),
                ephemeral=True
            )

        await interaction.followup.send(
            "VPS and attached data volumes permanently deleted.",
            ephemeral=True
        )


class CancelDeleteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary
        )

    async def callback(self, interaction):
        await interaction.response.send_message(
            "Deletion cancelled.",
            ephemeral=True
        )


# =====================================================
# MANAGE COMMAND
# =====================================================

@bot.command()
async def manage(ctx, *args):
    target_id = ctx.author.id
    number = None

    if args:
        mentioned_id = parse_mention(args[0])

        if mentioned_id is not None:
            target_id = mentioned_id
            args = args[1:]

            if target_id != ctx.author.id and not is_admin(ctx.author):
                return await reply_v2(
                    ctx, "Access denied",
                    "You cannot access another user's VPS."
                )

        elif args[0].isdigit() and not is_admin(ctx.author):
            number = int(args[0])
            args = args[1:]

        elif is_admin(ctx.author):
            return await reply_v2(
                ctx, "User required",
                "Admins must mention a user. Example: `<manage @user`"
            )
        else:
            return await reply_v2(
                ctx, "Invalid VPS number",
                "Use `<manage` or `<manage 1`."
            )

        if args:
            if len(args) != 1 or not args[0].isdigit() or int(args[0]) < 1:
                return await reply_v2(
                    ctx, "Invalid VPS number",
                    "Example: `<manage @user 1`"
                )
            number = int(args[0])

    elif is_admin(ctx.author):
        return await reply_v2(
            ctx, "User required",
            "Admins must mention a user. Example: `<manage @user`"
        )

    db = load_db()
    owned = [
        (ident, vps)
        for ident, vps in db.items()
        if int(vps.get("owner", 0)) == target_id
    ]

    used = {
        int(vps["number"])
        for _, vps in owned
        if str(vps.get("number", "")).isdigit()
    }

    next_num = 1
    changed = False
    numbered = []

    for ident, vps in owned:
        if str(vps.get("number", "")).isdigit():
            num = int(vps["number"])
        else:
            while next_num in used:
                next_num += 1
            num = next_num
            vps["number"] = num
            used.add(num)
            next_num += 1
            changed = True

        numbered.append((num, ident, vps))

    if changed:
        save_db(db)

    numbered.sort(key=lambda item: item[0])

    if number is not None:
        selected = next(
            (item for item in numbered if item[0] == number),
            None
        )

        if selected is None:
            available = ", ".join(str(item[0]) for item in numbered) or "none"
            return await reply_v2(
                ctx, "VPS not found",
                f"No VPS numbered `{number}` belongs to that user. Available: `{available}`."
            )

        _, ident, vps = selected

        if not can_manage(ctx.author, vps):
            return await reply_v2(ctx, "Access denied", "You cannot manage this VPS.")

        return await ctx.reply(view=VPSPanel(ident))

    if not numbered:
        return await reply_v2(ctx, "No VPS found", "This user has no VPS instances.")

    for _, ident, vps in numbered[:10]:
        if can_manage(ctx.author, vps):
            await ctx.reply(view=VPSPanel(ident))


@bot.command()
async def sshx(ctx, *args):
    try:
        target_id, ident = resolve_target(ctx, args)
    except PermissionError as exc:
        return await reply_v2(ctx, "Access denied", str(exc))

    if not ident:
        if target_id is None:
            return await reply_v2(
                ctx, "VPS ID required",
                "Use `<sshx VPS_ID` or `<sshx @user VPS_ID`."
            )

        candidates = user_vps(target_id)

        if len(candidates) == 1:
            ident = candidates[0][0]
        else:
            return await reply_v2(
                ctx, "VPS ID required",
                "Use `<sshx VPS_ID` or `<sshx @user VPS_ID`."
            )

    vps = get_vps(ident)

    if not vps:
        return await reply_v2(ctx, "VPS not found", "That VPS does not exist.")

    if target_id is not None and int(vps["owner"]) != target_id:
        return await reply_v2(ctx, "VPS not found", "That VPS does not belong to the selected user.")

    if not can_manage(ctx.author, vps):
        return await reply_v2(ctx, "Access denied", "You cannot access this VPS.")

    await reply_v2(ctx, "SSHX terminal", "Generating your private terminal link. Please wait...")

    try:
        url = await create_sshx_link(vps)

        try:
            await ctx.author.send(
                f"**KTS HOST SSHX Terminal**\n"
                f"VPS ID: `{ident}`\n{url}\n\nKeep this link private."
            )
            await reply_v2(ctx, "SSHX ready", "Your private terminal link has been sent to your DMs.")
        except discord.Forbidden:
            await reply_v2(ctx, "DM unavailable", "Enable DMs from this server and try again.")

    except Exception as exc:
        await reply_v2(ctx, "SSHX failed", f"```text\n{str(exc)[:1200]}\n```")


# =====================================================
# DELETE COMMAND
# =====================================================

@bot.command()
async def delete(ctx, *args):
    if not is_admin(ctx.author):
        return await reply_v2(
            ctx, "Access denied",
            "Only bot admins can delete VPS instances."
        )

    try:
        target_id, ident = resolve_target(ctx, args, admin_required=True)
    except PermissionError as exc:
        return await reply_v2(ctx, "Access denied", str(exc))

    if not ident:
        if target_id is None:
            return await reply_v2(
                ctx, "VPS ID required",
                "Use `<delete VPS_ID` or `<delete @user VPS_ID`."
            )

        candidates = user_vps(target_id)

        if len(candidates) == 1:
            ident = candidates[0][0]
        else:
            return await reply_v2(
                ctx, "VPS ID required",
                "Use `<delete VPS_ID` or `<delete @user VPS_ID`."
            )

    vps = get_vps(ident)

    if not vps:
        return await reply_v2(ctx, "VPS not found", "That VPS does not exist.")

    if target_id is not None and int(vps["owner"]) != target_id:
        return await reply_v2(
            ctx, "VPS not found",
            "That VPS does not belong to the selected user."
        )

    await ctx.reply(
        view=DeleteConfirmView(ident),
        mention_author=False
    )


# =====================================================
# ERROR HANDLING
# =====================================================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingRequiredArgument):
        return await reply_v2(
            ctx,
            "Missing arguments",
            "Create syntax: `<create @user RAM CPU STORAGE [DURATION]`\n"
            "Example: `<create @user 8 4 50 30d`\n"
            "Omit duration for unlimited VPS. Supported: 30s, 15m, 1d, 1mo, 1y, inf."
        )

    if isinstance(error, commands.BadArgument):
        return await reply_v2(
            ctx,
            "Invalid arguments",
            "Check the user mention and numeric VPS specifications."
        )

    await reply_v2(
        ctx,
        "Command error",
        f"```text\n{str(error)[:1200]}\n```"
    )


# =====================================================
# STARTUP
# =====================================================

@bot.event
async def on_ready():
    try:
        ensure_network()
    except Exception as exc:
        print(f"Docker network warning: {exc}")

    print(f"Logged in as {bot.user} ({bot.user.id})")
    print(f"Prefix: {PREFIX}")
    print("KTS HOST VPS Bot is online.")

    if not hasattr(bot, "expiry_task") or bot.expiry_task.done():
        bot.expiry_task = asyncio.create_task(expiry_worker())


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is missing from .env")

bot.run(TOKEN)
