import os, json, secrets, subprocess, asyncio, re, ipaddress
from pathlib import Path
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("PREFIX", "<")
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
MAX_RAM = float(os.getenv("MAX_RAM_GB", "32"))
MAX_CPU = float(os.getenv("MAX_CPU_CORES", "6"))
MAX_DISK = float(os.getenv("MAX_DISK_GB", "80"))
IMAGE = os.getenv("DOCKER_IMAGE", "kts-ubuntu-systemd:24.04")
DATA = Path(os.getenv("DATA_DIR", "/opt/kts-vps-bot/data"))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "vps.json"
NETWORK = os.getenv("VPS_NETWORK", "kts-vps-net")
SUBNET = ipaddress.ip_network(os.getenv("VPS_SUBNET", "172.30.0.0/16"), strict=False)
GATEWAY = os.getenv("VPS_GATEWAY", str(next(SUBNET.hosts())))
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

def run(args, check=True):
    return subprocess.run(["docker", *map(str, args)], text=True, capture_output=True, check=check)

def load():
    try: return json.loads(DB.read_text())
    except (OSError, json.JSONDecodeError): return {}

def save(db):
    tmp = DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2))
    tmp.replace(DB)

def is_admin(ctx):
    return ctx.author.id in ADMINS or bool(ctx.guild and ctx.author.guild_permissions.administrator)

def permitted(ctx, item):
    return bool(item) and (int(item["owner"]) == ctx.author.id or is_admin(ctx))

def get_item(ctx, ident):
    db = load()
    item = db.get(ident)
    return db, item if permitted(ctx, item) else None

def safe_id():
    return secrets.token_hex(4)

def ensure_network():
    found = run(["network", "inspect", NETWORK], check=False)
    if found.returncode == 0:
        return
    run(["network", "create", "--driver", "bridge", "--subnet", str(SUBNET),
         "--gateway", GATEWAY, NETWORK])

def allocate_ip(db):
    used = {v.get("private_ipv4") for v in db.values()}
    # Reserve .1 for gateway. Scan only a bounded portion of the configured subnet.
    for ip in SUBNET.hosts():
        s = str(ip)
        if s == GATEWAY or s in used:
            continue
        return s
    raise RuntimeError(f"No free IPs left in {SUBNET}")

async def reply_error(ctx, title, detail):
    await ctx.reply(f"❌ **{title}**\n```text\n{str(detail)[-1400:]}\n```")

@bot.event
async def on_ready():
    try:
        ensure_network()
        print(f"Network {NETWORK} ready: {SUBNET}, gateway {GATEWAY}")
    except Exception as e:
        print(f"Network setup failed: {e}")
    print(f"KTS HOST VPS bot online as {bot.user} ({bot.user.id})")

@bot.command(name="help")
async def help_cmd(ctx):
    e = discord.Embed(title="KTS HOST • VPS Control", color=0xE95420)
    e.description = (
        f"`{PREFIX}create @member RAM_GB CPU_CORES DISK_GB` — admin\n"
        f"`{PREFIX}myvps` · `{PREFIX}info ID` · `{PREFIX}connect ID`\n"
        f"`{PREFIX}start ID` · `{PREFIX}stop ID` · `{PREFIX}restart ID`\n"
        f"`{PREFIX}delete ID` · `{PREFIX}list` — admin\n\n"
        "Each VPS receives a unique private IPv4. Every VPS can bind the same internal ports."
    )
    await ctx.reply(embed=e)

@bot.command()
async def create(ctx, member: discord.Member, ram: float, cpu: float, disk: float = 10):
    if not is_admin(ctx): return await ctx.reply("Admin only.")
    if min(ram, cpu, disk) <= 0 or ram > MAX_RAM or cpu > MAX_CPU or disk > MAX_DISK:
        return await ctx.reply(f"Limits: RAM ≤ {MAX_RAM:g} GiB, CPU quota ≤ {MAX_CPU:g}, requested disk ≤ {MAX_DISK:g} GiB.")
    db = load()
    ident = safe_id()
    name = f"kts-vps-{ident}"
    volume = f"kts-data-{ident}"
    docker_volume = f"kts-docker-{ident}"
    try:
        ensure_network()
        private_ip = allocate_ip(db)
        run(["volume", "create", volume])
        run(["volume", "create", docker_volume])
        run(["run", "-d", "--name", name, "--hostname", name, "--restart", "unless-stopped",
             "--network", NETWORK, "--ip", private_ip,
             "--privileged",
             "--tmpfs", "/run:rw,nosuid,nodev", "--tmpfs", "/run/lock:rw,nosuid,nodev",
             "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw",
             "--memory", f"{ram}g", "--memory-swap", f"{ram}g", "--cpus", str(cpu),
             "--env", f"KTS_RAM_GIB={ram}", "--env", f"KTS_CPU_QUOTA={cpu}",
             "--env", f"KTS_DISK_REQUEST_GIB={disk}",
             "--pids-limit", "1024",
             "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
             "--cap-add", "FOWNER", "--cap-add", "SETGID", "--cap-add", "SETUID",
             "--cap-add", "SYS_ADMIN", "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW",
             "-v", f"{volume}:/opt/kts-storage",
             "-v", f"{docker_volume}:/var/lib/docker", IMAGE])
        # Confirm the init process remains running before saving the instance.
        await asyncio.sleep(2)
        state = run(["inspect", "-f", "{{.State.Status}}", name], check=False).stdout.strip()
        if state != "running":
            logs = run(["logs", "--tail", "80", name], check=False).stderr + run(["logs", "--tail", "80", name], check=False).stdout
            raise RuntimeError(f"Container did not stay running (state={state}). {logs}")
    except Exception as exc:
        run(["rm", "-f", name], check=False)
        run(["volume", "rm", volume], check=False)
        run(["volume", "rm", docker_volume], check=False)
        return await reply_error(ctx, "Provisioning failed", str(exc))
    db[ident] = {"owner": member.id, "name": name, "volume": volume, "ram_gib": ram,
                 "cpu_quota": cpu, "requested_disk_gib": disk, "private_ipv4": private_ip, "docker_volume": docker_volume,
                 "created_by": ctx.author.id}
    save(db)
    await ctx.reply(
        f"✅ Created VPS `{ident}` for {member.mention}\n"
        f"RAM: **{ram:g} GiB** · CPU quota: **{cpu:g}** · Requested disk: **{disk:g} GiB**\n"
        f"Private IPv4: **`{private_ip}`**\n"
        f"Every VPS has its own network namespace/IP, so it can use port **80** independently.\n"
        "This is a private address, not a public IPv4. Public access needs a relay/reverse proxy."
    )

@bot.command()
async def myvps(ctx):
    rows = [(k, v) for k, v in load().items() if int(v["owner"]) == ctx.author.id]
    if not rows: return await ctx.reply("No VPS found.")
    lines = [f"`{k}` · `{v.get('private_ipv4','legacy')}` · {v['ram_gib']:g} GiB RAM · {v['cpu_quota']:g} CPU" for k, v in rows]
    await ctx.reply("\n".join(lines)[:1900])

@bot.command()
async def info(ctx, ident):
    db, v = get_item(ctx, ident)
    if not v: return await ctx.reply("VPS not found or access denied.")
    state = run(["inspect", "-f", "{{.State.Status}}", v["name"]], check=False).stdout.strip() or "unknown"
    usage = "stopped"
    if state == "running":
        usage = run(["stats", "--no-stream", "--format", "{{.MemUsage}} RAM · {{.CPUPerc}} CPU", v["name"]], check=False).stdout.strip() or "unavailable"
    e = discord.Embed(title=f"KTS HOST VPS {ident}", color=0x2ECC71 if state == "running" else 0x95A5A6)
    e.add_field(name="Status", value=state, inline=True)
    e.add_field(name="Private IPv4", value=v.get("private_ipv4", "legacy / unassigned"), inline=True)
    e.add_field(name="Live usage", value=usage, inline=True)
    e.add_field(name="RAM limit", value=f"{v['ram_gib']:g} GiB", inline=True)
    e.add_field(name="CPU quota", value=str(v["cpu_quota"]), inline=True)
    e.add_field(name="Storage", value=f"{v['requested_disk_gib']:g} GiB requested; hard quota not active", inline=False)
    await ctx.reply(embed=e)

async def lifecycle(ctx, ident, action):
    db, v = get_item(ctx, ident)
    if not v: return await ctx.reply("VPS not found or access denied.")
    result = run([action, v["name"]], check=False)
    if result.returncode: return await reply_error(ctx, f"{action} failed", result.stderr or result.stdout)
    await ctx.reply(f"✅ `{ident}`: `{action}` completed.")

@bot.command()
async def start(ctx, ident): await lifecycle(ctx, ident, "start")
@bot.command()
async def stop(ctx, ident): await lifecycle(ctx, ident, "stop")
@bot.command()
async def restart(ctx, ident): await lifecycle(ctx, ident, "restart")

@bot.command()
async def delete(ctx, ident):
    db, v = get_item(ctx, ident)
    if not v: return await ctx.reply("VPS not found or access denied.")
    if not is_admin(ctx): return await ctx.reply("Only admins can delete VPS instances.")
    result = run(["rm", "-f", v["name"]], check=False)
    if result.returncode: return await reply_error(ctx, "Delete failed", result.stderr)
    db.pop(ident, None)
    save(db)
    await ctx.reply(f"🗑️ Container `{ident}` deleted. Volume `{v['volume']}` retained to protect customer data.")

@bot.command()
async def list(ctx):
    if not is_admin(ctx): return await ctx.reply("Admin only.")
    db = load()
    if not db: return await ctx.reply("No VPS instances.")
    lines = [f"`{k}` · <@{v['owner']}> · `{v.get('private_ipv4','legacy')}` · {v['ram_gib']:g}GiB · {v['cpu_quota']:g} CPU" for k, v in db.items()]
    for i in range(0, len(lines), 20):
        await ctx.reply("\n".join(lines[i:i+20])[:1900])

@bot.command()
async def connect(ctx, ident):
    db, v = get_item(ctx, ident)
    if not v: return await ctx.reply("VPS not found or access denied.")
    state = run(["inspect", "-f", "{{.State.Status}}", v["name"]], check=False).stdout.strip()
    if state != "running":
        result = run(["start", v["name"]], check=False)
        if result.returncode: return await reply_error(ctx, "Could not start VPS", result.stderr)
    script = "mkdir -p /run/kts; rm -f /run/kts/sshx.log; nohup sh -c 'script -q -f -c sshx /run/kts/sshx.log' >/dev/null 2>&1 &"
    result = run(["exec", v["name"], "bash", "-lc", script], check=False)
    if result.returncode: return await reply_error(ctx, "Could not launch terminal", result.stderr)
    for _ in range(30):
        await asyncio.sleep(.5)
        p = run(["exec", v["name"], "bash", "-lc", "cat /run/kts/sshx.log 2>/dev/null || true"], check=False)
        clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", p.stdout)
        urls = re.findall(r"https://[^\s<>\"']+", clean)
        if urls:
            url = urls[-1].rstrip(".,);]")
            try:
                await ctx.author.send(f"**KTS HOST terminal** · VPS `{ident}`\n{url}\n\nKeep this private.")
                return await ctx.reply("✅ Terminal link sent to your DMs.")
            except discord.Forbidden:
                return await ctx.reply("I couldn't DM you. Enable DMs and try again.")
    await reply_error(ctx, "SSHX did not return a link", "Check the SSHX installer and container logs.")

if not TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in .env before starting.")
bot.run(TOKEN)
