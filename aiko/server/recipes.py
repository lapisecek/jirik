"""Recepty: pevné postupy opravy pro každý záměr.

Recept nejdřív jen *diagnostikuje* (nic nemění) a vrátí buď odpověď,
nebo plán kroků. Úroveň bezpečnosti je pevně v kódu receptu a nástrojů.
LLM ji nemůže změnit ani obejít.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from ..common.safety import Level, is_blocked_package, is_blocked_url
from .brain import Understanding
from .config import Config, Contact


class RecipeFailed(Exception):
    pass


@dataclass
class Step:
    tool: str
    args: dict[str, Any]
    narrate: str               # co Aiko řekne během akce
    timeout: float = 30.0


@dataclass
class Plan:
    confirm: str                                   # co přesně udělá (před "ano")
    steps: list[Step]
    done: str | Callable[[list[Any]], str]         # shrnutí po úspěchu
    log: str                                       # záznam pro rodinu (bez obsahu zpráv)
    level: Level = Level.ORANGE
    fallback: list[Step] = field(default_factory=list)   # druhý pokus jinou cestou
    fallback_done: str = ""
    server_action: str | None = None               # "notify_family" | "clear_log" | "undo" | "agent"
    notify_text: str = ""
    undo: list[Step] = field(default_factory=list)  # jak změnu vrátit („vrať to zpátky“)
    undo_label: str = ""                           # „zvětšení písma“

    def summary(self, results: list[Any]) -> str:
        return self.done(results) if callable(self.done) else self.done


@dataclass
class Answer:
    """Odpověď bez zásahu do zařízení."""
    say: str
    log: str = ""
    kind: str = "info"           # info | refused | fail | alert
    notify: bool = False
    ask_slot: str | None = None  # "app" | "contact" | "text" – Aiko se doptá
    offer_family: bool = False   # nabídne poslat zprávu rodině


@dataclass
class Ctx:
    cfg: Config
    u: Understanding
    device: Any
    call: Callable[[str, dict[str, Any]], Awaitable[Any]]


class Recipe:
    intent: str = ""
    level: Level = Level.ORANGE
    kinds: tuple[str, ...] = ("phone",)

    def targets(self, u: Understanding) -> tuple[str, ...]:
        return self.kinds

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:  # pragma: no cover
        raise NotImplementedError


def refuse_protected(cfg: Config, what: str) -> Answer:
    return Answer(
        say=f"Tohle neudělám. {what} je chráněné, tam Aiko nikdy nesahá. Dávám vědět {cfg.family.to_whom}.",
        log=f"Odmítla zásah do chráněné aplikace ({what})", kind="refused", notify=True)


SITE_NAMES = {"seznamzpravy.cz": "Seznam Zprávy", "pocasi.seznam.cz": "Počasí na Seznamu",
              "email.seznam.cz": "E-mail na Seznamu", "seznam.cz": "Seznam", "novinky.cz": "Novinky",
              "idnes.cz": "iDNES", "irozhlas.cz": "iROZHLAS", "youtube.com": "YouTube", "google.com": "Google",
              "news.google.com": "Zprávy Google", "mapy.cz": "Mapy", "idos.cz": "Jízdní řády",
              "ct24.ceskatelevize.cz": "ČT24", "ceskatelevize.cz": "Česká televize", "mail.google.com": "Gmail"}


def site_label(url: str) -> str:
    host = (urlparse(url).netloc or url).lower().removeprefix("www.")
    for h, name in SITE_NAMES.items():
        if host == h:
            return name
    name = host.split(".")[0]
    return name.capitalize() if name else "Stránku"


# ---------------------------------------------------------------------------

class AppMissing(Recipe):
    intent, level, kinds = "app_missing", Level.ORANGE, ("phone",)

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        pkg, cfg = ctx.u.app, ctx.cfg
        if not pkg:
            return Answer("Kterou aplikaci myslíte? Řekněte prosím její jméno.", ask_slot="app")
        name = ctx.u.app_name or pkg
        if is_blocked_package(pkg):
            return refuse_protected(cfg, name)
        st = await ctx.call("check_app_state", {"package_name": pkg})
        state = st.get("state")
        # „aplikace X“ (ženský rod) sedí gramaticky na WhatsApp, Zprávy, Fotky i Počasí
        app = f"Aplikace {name}"
        restore = Step("restore_and_launch_app", {"package_name": pkg},
                       f"Vracím aplikaci {name} zpátky. Chvilku strpení.")
        store = Step("install_package_silent", {"apk_or_id": pkg}, "Otevírám obchod Play.", timeout=120)

        def store_done(results: list[Any]) -> str:
            last = results[-1] if results else {}
            if isinstance(last, dict) and last.get("mode") == "installed":
                return f"Hotovo. {app} je nainstalovaná a otevřená."
            return "Obchod je otevřený. Klepněte prosím na zelené tlačítko Instalovat."

        if state == "missing":
            return Plan(
                confirm=f"{app} v telefonu není. Mám otevřít obchod, abyste {cfg.g('ji mohla', 'ji mohl')} "
                        f"nainstalovat?",
                steps=[store], done=store_done, log=f"Otevřela obchod Play s aplikací {name}")
        confirm = {
            "ok": f"{app} v telefonu je a funguje. Možná jen zmizela z plochy. Mám ji otevřít?",
            "disabled": f"{app} je v telefonu vypnutá. Mám ji zapnout a otevřít?",
            "archived": f"{app} se uspala, aby šetřila místo. Mám ji probudit a otevřít?",
            "hidden": f"{app} se v telefonu schovala. Mám ji vrátit a otevřít?",
        }.get(state, f"{app} se nějak zasekla. Mám ji zkusit vrátit a otevřít?")
        return Plan(confirm=confirm, steps=[restore],
                    done=f"Hotovo. {app} je zpátky a je otevřená na obrazovce.",
                    log=f"Vrátila aplikaci {name} na obrazovku ({state})", fallback=[store],
                    fallback_done="Vrátit ji se nepodařilo, tak jsem otevřela obchod. Klepněte prosím na Instalovat.")


class OpenApp(Recipe):
    intent, level, kinds = "open_app", Level.GREEN, ("phone",)

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        pkg = ctx.u.app
        if not pkg:
            return Answer("Kterou aplikaci mám otevřít?", ask_slot="app")
        name = ctx.u.app_name or pkg
        if is_blocked_package(pkg):
            return refuse_protected(ctx.cfg, name)
        st = await ctx.call("check_app_state", {"package_name": pkg})
        if st.get("state") != "ok":
            return await AppMissing().diagnose(ctx)
        return Plan(confirm="", level=Level.GREEN,
                    steps=[Step("launch_app", {"package_name": pkg}, f"Otevírám aplikaci {name}.")],
                    done=f"Aplikace {name} je otevřená.", log=f"Otevřela aplikaci {name}")


class SoundLow(Recipe):
    intent, level, kinds = "sound_low", Level.ORANGE, ("phone", "pc")

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        st = await ctx.call("get_audio_state", {})
        if ctx.device.kind == "pc":
            vol = st.get("volume")
            if not st.get("unknown") and not st.get("muted") and (vol is None or vol >= 40):
                return Answer("Zvuk na počítači je zapnutý.",
                              offer_family=True)
            return Plan(confirm=("Zkusím zvuk na počítači zesílit. Mám?" if st.get("unknown")
                                 else "Zvuk na počítači je ztlumený. Mám ho zesílit?"),
                        steps=[Step("fix_sound", {}, "Zesiluji zvuk.")],
                        done="Hotovo. Zvuk na počítači je zase nahlas.", log="Zesílila zvuk na počítači")
        problems = []
        if st.get("ringer_mode") in ("silent", "vibrate"):
            problems.append("Telefon je ztlumený")
        if st.get("dnd"):
            problems.append("Je zapnutý režim Nerušit")
        ring, ring_max = st.get("ring", 0), st.get("ring_max", 7) or 7
        music, music_max = st.get("music", 0), st.get("music_max", 15) or 15
        if ring < ring_max * 0.5 or music < music_max * 0.4:
            problems.append("Zvuk je potichu")
        if not problems:
            return Answer("Zvuk v telefonu je zapnutý a nahlas.",
                          offer_family=True)
        return Plan(confirm=". ".join(problems) + ". Mám zvuk zapnout a zesílit?",
                    steps=[Step("set_volume_normal", {}, "Zapínám zvuk.")],
                    done="Hotovo. Zvuk je zase zapnutý a nahlas.", log="Zapnula a zesílila zvuk v telefonu")


class NoInternet(Recipe):
    intent, level, kinds = "no_internet", Level.ORANGE, ("phone", "pc")

    def targets(self, u: Understanding) -> tuple[str, ...]:
        return ("pc", "phone") if u.url else ("phone", "pc")

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        fam = ctx.cfg.family.to_whom
        url = ctx.u.url
        if url and is_blocked_url(url):
            return refuse_protected(ctx.cfg, "Tahle stránka")
        if ctx.device.kind == "pc":
            d = await ctx.call("diagnose_network", {"url": url} if url else {})
            verdict = d.get("verdict")
            label = site_label(url) if url else ""
            open_step = [Step("open_and_fix_browser", {"url": url}, f"Otevírám {label}.")] if url else []
            if verdict == "ok":
                if url:
                    return Plan(confirm="", level=Level.GREEN, steps=open_step,
                                done=f"Internet funguje. {label} máte otevřený na obrazovce.",
                                log=f"Otevřela {label} na počítači")
                return Answer("Internet na počítači funguje.", log="Zkontrolovala internet na počítači – v pořádku")
            if verdict == "router":
                return Answer(f"Nefunguje domácí router, ta krabička se světýlky. Zkuste ji vypnout a zapnout. "
                              f"Dávám vědět {fam}.", log="Nefunguje domácí router", kind="fail", notify=True)
            if verdict == "isp":
                return Answer(f"Internet nejde v celém domě. Nejspíš je výpadek u poskytovatele. Dávám vědět {fam}.",
                              log="Výpadek internetu u poskytovatele", kind="fail", notify=True)
            if verdict == "site":
                return Answer(f"Internet funguje, jen {label or 'ta stránka'} teď nejde. Zkuste to prosím později.",
                              log=f"Stránka {label} nefunguje (internet v pořádku)")
            reason = {
                "dns": "Počítač je připojený, ale stránky nenajde",
                "no_ip": "Počítač se nepřipojil k síti",
                "no_adapter": "Počítač není připojený k síti",
            }.get(verdict, "Počítač má potíž se sítí")
            repair = Step("atomic_network_repair", {"set_fallback_dns": True},
                          "Opravuji nastavení sítě. Chvíli to potrvá.", timeout=120)
            done = "Opravila jsem nastavení sítě."
            if url:
                done += f" {label} už máte otevřený na obrazovce."
            return Plan(confirm=f"{reason}. Mám opravit nastavení sítě?", steps=[repair] + open_step,
                        done=done, log=f"Opravila síť na počítači ({verdict})")
        # telefon
        s = await ctx.call("get_connectivity_state", {})
        if s.get("internet_ok"):
            return Answer("Internet v telefonu funguje.", log="Zkontrolovala internet v telefonu – v pořádku")
        problems = []
        if s.get("airplane"):
            problems.append("Telefon je v režimu letadlo")
        if not s.get("wifi_enabled"):
            problems.append("Wi-Fi je vypnutá")
        reason = ". ".join(problems) or "Telefon nemá internet"

        def done(results: list[Any]) -> str:
            last = results[-1] if results else {}
            if isinstance(last, dict) and last.get("internet_ok") is False:
                raise RecipeFailed("internet stále nejde")
            return "Hotovo. Internet v telefonu zase funguje."

        return Plan(confirm=f"{reason}. Zkusím internet zapnout. Mám?",
                    steps=[Step("reset_connectivity", {"keep_wifi": True}, "Zapínám internet.", timeout=60)],
                    done=done, log="Obnovila připojení telefonu")


class OpenWebsite(Recipe):
    intent, level, kinds = "open_website", Level.GREEN, ("pc", "phone")

    def targets(self, u: Understanding) -> tuple[str, ...]:
        # „na mobilu otevři Seznam“ – dřív šlo vždy na počítač, a když nebyl zapnutý, Aiko to vzdala
        return ("phone", "pc") if u.target == "phone" else ("pc", "phone")

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        url = ctx.u.url
        if not url:
            return Answer("Kterou stránku mám otevřít?", ask_slot="url")
        if is_blocked_url(url):
            return refuse_protected(ctx.cfg, "Tahle stránka")
        label = site_label(url)
        tool = "open_and_fix_browser" if "open_and_fix_browser" in (ctx.device.tools or {}) else "open_url"
        where = "v telefonu" if ctx.device.kind == "phone" else "na počítači"
        return Plan(confirm="", level=Level.GREEN,
                    steps=[Step(tool, {"url": url}, f"Otevírám {label} {where}.")],
                    done=f"Hotovo. {label} máte otevřený {where}.", log=f"Otevřela {label} {where}")


async def resolve_contact(ctx: Ctx, question: str) -> Contact | Answer:
    """Najde adresáta: 1) rodina z aiko.toml, 2) kontakty v telefonu (JSON se shodami), 3) doptá se."""
    u = ctx.u
    if u.contact_phone:                                   # už vybráno z kontaktů telefonu
        return Contact(u.contact, phone=u.contact_phone)
    c = ctx.cfg.find_contact(u.contact) if u.contact else None
    if c and c.phone:
        return c
    if not u.contact:
        return Answer(question, ask_slot="contact")
    if "find_contacts" not in (ctx.device.tools or {}):
        return Answer(f"{question} Jméno {u.contact} neznám.", ask_slot="contact")
    res = await ctx.call("find_contacts", {"query": u.contact})
    matches = res.get("matches", []) if isinstance(res, dict) else []
    if len(matches) == 1:
        return Contact(matches[0]["name"], phone=matches[0]["phone"])
    if matches:
        u.candidates = matches[:4]
        names = ", nebo ".join(m["name"] for m in u.candidates)
        return Answer(f"V telefonu mám víc lidí: {names}. Kterého myslíte?", ask_slot="contact")
    sugg = res.get("suggestions", []) if isinstance(res, dict) else []
    if sugg:
        # Dřív tu Aiko jen řekla „nenašla jsem“ a nic se nestalo – teď nabídne podobná jména
        u.candidates = sugg[:3]
        names = ", nebo ".join(m["name"] for m in u.candidates)
        return Answer(f"Přesně {u.contact} v kontaktech nemám. Myslíte {names}? Řekněte jméno.",
                      ask_slot="contact")
    return Answer(f"V kontaktech jsem {u.contact} nenašla. {question} Řekněte prosím jméno, jak ho máte "
                  "v telefonu uložené.", ask_slot="contact")


def _to(c: Contact, prefix: str) -> str:
    """„Napíšu Lence“ u rodiny (známe 3. pád), jinak „Napíšu: Lenka Nováková“."""
    return f"{prefix} {c.dative}." if c.dative else f"{prefix}. Komu: {c.name}."


def _log_name(c: Contact) -> str:
    # jména z kontaktů telefonu do záznamu rodiny nepíšeme (kontakty zůstávají v telefonu)
    return c.name if c.dative else "kontakt z telefonu"


class SendMessage(Recipe):
    intent, level, kinds = "send_message", Level.ORANGE, ("phone",)

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        c = await resolve_contact(ctx, "Komu mám napsat?")
        if isinstance(c, Answer):
            return c
        text = ctx.u.text.strip()
        if not text:
            return Answer(f"Co mám {c.dative} napsat?" if c.dative else f"Co mám napsat? Komu: {c.name}.",
                          ask_slot="text")
        if ctx.u.channel == "sms":
            return Plan(
                confirm=f"{_to(c, 'Napíšu esemesku')} Text: {text}. Odeslat ji pak zmáčknete vy. Mám ji připravit?",
                steps=[Step("open_sms_draft", {"phone": c.phone, "text": text}, "Připravuji esemesku.")],
                done="Esemeska je připravená. Odešlete ji šipkou vedle textu.",
                log=f"Připravila SMS ({_log_name(c)})")
        return Plan(
            confirm=f"{_to(c, 'Napíšu přes WhatsApp')} Text: {text}. Odeslat to pak zmáčknete vy. "
                    f"Mám zprávu připravit?",
            steps=[Step("open_whatsapp_chat", {"phone": c.phone, "text": text}, "Připravuji zprávu.")],
            done="Zpráva je připravená. Odešlete ji zelenou šipkou vpravo dole.",
            log=f"Připravila zprávu ve WhatsAppu ({_log_name(c)})")


class CallPerson(Recipe):
    intent, level, kinds = "call_person", Level.ORANGE, ("phone",)

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        c = await resolve_contact(ctx, "Komu mám zavolat?")
        if isinstance(c, Answer):
            return c
        who = c.dative or c.name
        return Plan(confirm=f"Mám zavolat {c.dative}?" if c.dative else f"Mám zavolat: {c.name}?",
                    steps=[Step("call_number", {"phone": c.phone}, f"Volám: {who}.")],
                    done=f"Volám: {who}.", log=f"Zavolala ({_log_name(c)})")


class TextTooSmall(Recipe):
    intent, level, kinds = "text_too_small", Level.ORANGE, ("phone",)

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        st = await ctx.call("get_font_scale", {})
        scale = float(st.get("scale") or 1.0)
        if scale >= 1.5:
            return Answer("Písmo už je největší, jaké jde.",
                          offer_family=True)
        new = round(min(1.5, scale + 0.3), 2)
        return Plan(confirm="Zvětším vám písmo v telefonu. Mám?",
                    steps=[Step("set_font_scale", {"scale": new}, "Zvětšuji písmo.")],
                    done="Hotovo. Písmo je teď větší. Když bude moc velké, řekněte vrať to.",
                    log=f"Zvětšila písmo ({scale} → {new})",
                    undo=[Step("set_font_scale", {"scale": scale}, "Vracím písmo.")], undo_label="zvětšení písma")


class TextTooBig(Recipe):
    intent, level, kinds = "text_too_big", Level.ORANGE, ("phone",)

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        st = await ctx.call("get_font_scale", {})
        scale = float(st.get("scale") or 1.0)
        if scale <= 0.85:
            return Answer("Písmo už je nejmenší, jaké jde.")
        new = 1.0 if scale > 1.0 else 0.85
        return Plan(confirm=("Vrátím písmo na běžnou velikost. Mám?" if new == 1.0
                             else "Zmenším vám písmo v telefonu. Mám?"),
                    steps=[Step("set_font_scale", {"scale": new}, "Zmenšuji písmo.")],
                    done="Hotovo. Písmo je teď menší.", log=f"Zmenšila písmo ({scale} → {new})",
                    undo=[Step("set_font_scale", {"scale": scale}, "Vracím písmo.")], undo_label="zmenšení písma")


class Battery(Recipe):
    intent, level, kinds = "battery", Level.GREEN, ("phone",)

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        b = await ctx.call("get_battery", {})
        level = int(b.get("level", 0))
        say = f"Baterie má {level} procent."
        if b.get("charging"):
            say += " Telefon se právě nabíjí."
        elif level < 20:
            say += " Dejte prosím telefon nabít."
        if not b.get("power_save") and level < 50:
            say += " Pomůže úsporný režim. Najdete ho v nastavení, v části Baterie."
        return Answer(say, log=f"Zkontrolovala baterii ({level} %)")


class ScamCheck(Recipe):
    intent, level, kinds = "scam_check", Level.RED, ()

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        cfg = ctx.cfg
        return Answer(
            say=(f"Tohle neudělám. Banka ani policie nikdy nechce, abyste si "
                 f"{cfg.g('něco instalovala', 'něco instaloval')} nebo říkal{cfg.g('a', '')} kódy. "
                 f"Položte prosím telefon. Dávám vědět {cfg.family.to_whom}."),
            log="Možný pokus o podvod – Aiko odmítla a varovala", kind="alert", notify=True)


class PlayMinesweeper(Recipe):
    """Hra Hledání min: oficiální Google Minesweeper v prohlížeči, hraje nástroj play_minesweeper na PC
    (čte plochu ze snímku a kliká jen na bezpečná políčka). Vision model k tomu není potřeba."""
    intent, level, kinds = "play_minesweeper", Level.ORANGE, ("pc",)

    async def diagnose(self, ctx: Ctx) -> Plan | Answer:
        if "play_minesweeper" not in (getattr(ctx.device, "tools", None) or {}):
            return Answer("Hledání min na tomhle počítači zatím hrát neumím. Je potřeba aktualizovat Aiko "
                          "na počítači.", log="Hledání min: počítač nemá nástroj play_minesweeper", kind="fail")

        def done(results: list[Any]) -> str:
            from ..common.minesweeper import summary_cs

            res = results[0] if results and isinstance(results[0], dict) else {}
            return summary_cs(res)

        return Plan(
            confirm=("Otevřu na počítači hru Hledání min od Googlu a zahraju ji. Když pohnete myší, "
                     "hned přestanu. Mám začít?"),
            steps=[Step("play_minesweeper", {"max_seconds": 90}, "Otevírám Hledání min a hraju.", timeout=110)],
            done=done, log="Hrála na počítači hru Hledání min", level=Level.ORANGE)


RECIPES: dict[str, Recipe] = {r.intent: r for r in (
    AppMissing(), OpenApp(), SoundLow(), NoInternet(), OpenWebsite(),
    SendMessage(), CallPerson(), TextTooSmall(), TextTooBig(), Battery(), ScamCheck(), PlayMinesweeper(),
)}
