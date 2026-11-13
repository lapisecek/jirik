"""Bezpečnostní pravidla, která platí na serveru i v démonech na zařízeních.

Úroveň akce je pevně v kódu. LLM ji nemůže změnit ani obejít.
Démony tento modul používají jako "hardcoded blocklist" (TDD kap. 6).
"""
from __future__ import annotations

import re
import unicodedata
from enum import IntEnum


class Level(IntEnum):
    GREEN = 0   # provede hned, ale nahlas řekne, co dělá
    ORANGE = 1  # nejdřív řekne, co udělá, a čeká na "ano"
    RED = 2     # nikdy – odmítne a upozorní rodinu

    @property
    def label(self) -> str:
        return {0: "zelená", 1: "oranžová", 2: "červená"}[int(self)]


# Autoritativní úrovně nástrojů. Démon může úroveň jen zvýšit, nikdy snížit.
# Neznámý nástroj = ORANGE.
TOOL_LEVELS: dict[str, Level] = {
    # telefon
    "check_app_state": Level.GREEN,
    "launch_app": Level.GREEN,
    "restore_and_launch_app": Level.ORANGE,
    "install_package_silent": Level.ORANGE,
    "get_connectivity_state": Level.GREEN,
    "reset_connectivity": Level.ORANGE,
    "get_audio_state": Level.GREEN,
    "set_volume_normal": Level.ORANGE,
    "get_font_scale": Level.GREEN,
    "set_font_scale": Level.ORANGE,
    "get_battery": Level.GREEN,
    "open_whatsapp_chat": Level.ORANGE,
    "open_sms_draft": Level.ORANGE,
    "find_contacts": Level.GREEN,
    "call_number": Level.ORANGE,
    "get_ui_tree": Level.GREEN,
    "take_screenshot": Level.GREEN,
    "list_apps": Level.GREEN,
    "open_url": Level.GREEN,
    "open_settings": Level.GREEN,
    "create_folder": Level.ORANGE,
    "system_info": Level.GREEN,
    "focus_window": Level.GREEN,
    # počítač
    "diagnose_network": Level.GREEN,
    "atomic_network_repair": Level.ORANGE,
    "open_and_fix_browser": Level.GREEN,
    "fix_sound": Level.ORANGE,
    "set_desktop_icons": Level.ORANGE,
    # soubory, aplikace, tiskárny (skilly) – čtení zeleně, změny oranžově (celý úkol senior potvrzuje „ano“)
    "list_files": Level.GREEN,
    "find_files": Level.GREEN,
    "move_files": Level.ORANGE,
    "open_item": Level.GREEN,
    "print_file": Level.ORANGE,
    "start_app": Level.GREEN,
    "list_printers": Level.GREEN,
    "pc_health": Level.GREEN,
    # hra Hledání min – kliká jen uvnitř hrací plochy v prohlížeči (senior potvrzuje „ano“)
    "play_minesweeper": Level.ORANGE,
    # GUI agent (TDD v2) – telefon i počítač
    "observe_screen": Level.GREEN,
    "shell_read": Level.GREEN,
    "execute_action": Level.ORANGE,
    "run_macro": Level.ORANGE,
}

# Nástroje, které GUI agent (VLM) nesmí volat sám – hovory a zprávy jdou jen přes
# recepty s důvěryhodnými kontakty; snímání a akce řídí smyčka agenta.
AGENT_FORBIDDEN_TOOLS = frozenset({
    "call_number", "open_whatsapp_chat", "open_sms_draft", "observe_screen", "execute_action", "run_macro",
    "get_ui_tree", "take_screenshot",
})

# Prvky na obrazovce, na které agent nikdy neklikne (červená úroveň v kódu).
# „Odeslat“ zmáčkne vždy senior sám – agent smí zprávu jen připravit.
_RISKY_LABEL = re.compile(
    r"\bodeslat\b|\bodesl\w*|\bsend\b|\bvolat\b|\bzavolat\b|\bhovor\b|\bcall\b|"
    r"odinstal|uninstall|\bsmaz|vymaz|\bdelete\b|clear (data|storage|cache)|tovarn|factory|"
    r"\breset\w*|zaplat|platb|\bplatit|koupit|\bkoupe|\bbuy\b|\bpay\b|purchase|predplat|subscri|"
    r"vyvojar|developer|heslo|password|\bpin\b|anydesk|teamviewer|rustdesk|vzdalen\w* pristup|"
    r"odhlasit|sign out|log ?out|zablokovat ucet|smlouv|souhlasim s podminkami|"
    # vypnutí / restart počítače – senior by pak nevěděl, co se stalo, a agent by přišel o spojení
    r"\bshut ?down\b|\brestart\w*|vypnout (pocitac|zarizeni|telefon)|\bnapajeni\b|\buspat\b|\bsleep\b|"
    r"aktualizovat a (vypnout|restartovat)|naformat|formatovat (disk|jednotk)|"
    r"^(vypnout|power off)( menuitem| button| listitem|$)|rezim spanku")


def is_risky_label(label: str | None) -> bool:
    return bool(label) and bool(_RISKY_LABEL.search(normalize(label)))


# --- Počítač: klávesové zkratky a systémová okna -------------------------------------------

# Win+R (Spustit), Win+X (nabídka správce), Ctrl+Shift+Esc (Správce úloh), Ctrl+Alt+Del, Win+L (zamknutí –
# odemknout musí senior heslem), Alt+F4 na ploše = dialog Vypnout Windows (ten řeší pc.py podle okna)
_FORBIDDEN_KEYS = re.compile(r"^(?:(?:win|cmd|super|meta)\+(?:r|x|l)|ctrl\+shift\+esc|ctrl\+alt\+(?:del|delete|end)|"
                             r"alt\+ctrl\+(?:del|delete)|(?:win|super)\+ctrl\+.*)$")


def _key_combo(key: str) -> str:
    return "+".join(p for p in re.sub(r"\s+", "", (key or "").lower()).split("+") if p)


def is_forbidden_key(key: str | None) -> bool:
    return bool(key) and bool(_FORBIDDEN_KEYS.match(_key_combo(key)))


# Okna, kde by psaní nebo kliknutí mohlo spustit příkaz nebo změnit systém. Agent v nich smí jen zavřít okno.
_SYSTEM_WINDOW = re.compile(
    r"windows powershell|\bpowershell\b|\bpwsh\b|command prompt|prikazovy radek|\bcmd(\.exe)?\b|terminal\b|"
    # vývojářské programy s vestavěným terminálem (živě: agent ve VS Code klikal na „Focus Terminal“)
    r"visual studio code|visual studio\b|\bcursor\b|pycharm|intellij|android studio|\bgit bash\b|\bwsl\b|"
    r"\bspravce:|\badministrator:|editor registru|registry editor|\bregedit\b|^spustit$|^run$|"
    r"spravce uloh|task manager|zasady skupiny|group policy|\bsluzby$|^services$|konfigurace systemu|"
    r"system configuration|spravce disku|disk management|spravce zarizeni|device manager|"
    r"kontrola uzivatelskych uctu|user account control|bitlocker|windows defender|zabezpeceni windows|"
    r"windows security|ochrana pred viry|virus threat")
_BROWSER_TITLE = re.compile(r" (google chrome|microsoft edge|mozilla firefox|opera|brave|vivaldi|seznam prohlizec)$")


def is_system_window(title: str | None) -> bool:
    t = normalize(title or "")
    return bool(t) and not _BROWSER_TITLE.search(t) and bool(_SYSTEM_WINDOW.search(t))


# Text, který by v hledání Start / adresním řádku spustil konzoli nebo systémový nástroj.
_LAUNCH_TEXT = re.compile(r"^\s*(cmd|powershell|pwsh|wt|regedit|gpedit|msconfig|diskpart|taskmgr|mmc|"
                          r"rundll32|mshta|wscript|cscript|certutil|bitsadmin|shutdown|"
                          r"netsh|reg|sc|bcdedit|wmic|curl|iwr|invoke-\w+)(\.exe|\.msc)?(\s|$)", re.IGNORECASE)


def is_dangerous_text(text: str | None) -> bool:
    """Psaní, které vypadá jako spuštění systémového příkazu (Start → „powershell“ → Enter)."""
    t = (text or "").strip()
    return bool(_LAUNCH_TEXT.match(t)) or t.lower().startswith(("javascript:", "file:", "ms-msdt:", "search-ms:"))


_CODE_IN_TEXT = re.compile(r"(?<![\d+])\d(?:[ -]?\d){3,9}(?!\d)")


def redact_codes(text: str) -> str:
    """Ověřovací kódy, PINy a čísla karet z předčítaného textu nahradí slovem „(kód skrytý)“.

    Design: Aiko nikdy nepřepisuje ani nepředčítá 2FA kódy z SMS. Telefonní čísla (+420…) a
    letopočty/časy s oddělovači (12:30, 2026) se ponechají jen tehdy, když nejde o 4–10 číslic za sebou.
    """
    return _CODE_IN_TEXT.sub("(kód skrytý)", text or "")


def looks_like_code(text: str) -> bool:
    """PIN, ověřovací kód z SMS, číslo karty… – agent je nikdy nepíše."""
    t = (text or "").replace(" ", "").replace("-", "")
    return bool(re.fullmatch(r"\d{4,}", t))


def tool_level(name: str, declared: int | None = None) -> Level:
    base = TOOL_LEVELS.get(name, Level.ORANGE)
    if declared is None:
        return base
    return Level(max(int(base), int(declared)))


# --- Blocklist aplikací (bankovnictví, platby, hesla, vzdálený přístup) -------

BLOCKED_PACKAGES: frozenset[str] = frozenset({
    # české banky
    "cz.csas.georgego", "cz.csas.george", "cz.csob.smartbanking", "cz.csob.smart",
    "cz.kb.mobilebanking", "cz.kb.plus", "cz.airbank.android", "cz.moneta.smartbanka",
    "cz.fio.sb2", "cz.rb.app.smartphonebanking", "cz.mbank", "cz.unicreditbank.smartbanking",
    # platby a peněženky
    "com.revolut.revolut", "com.paypal.android.p2pmobile", "com.google.android.apps.walletnfcrel",
    # hesla a 2FA
    "com.x8bit.bitwarden", "com.lastpass.lpandroid", "com.agilebits.onepassword",
    "com.google.android.apps.authenticator2", "com.azure.authenticator", "com.authy.authy",
    # vzdálený přístup
    "com.anydesk.anydeskandroid", "com.teamviewer.quicksupport.market",
    "com.teamviewer.teamviewer.market.mobile", "com.rustdesk.rustdesk",
    "com.microsoft.rdc.androidx", "com.splashtop.remote.pad.v2",
    # nastavení pro vývojáře / systémová bezpečnost
    "com.android.settings.development",
})

_BLOCKED_SUBSTRINGS = (
    "bank", "banka", "banking", "wallet", "authenticator", "password", "passwd",
    "anydesk", "teamviewer", "rustdesk", "remotedesktop", "quicksupport", "airdroid",
)

BLOCKED_DOMAINS = (
    "anydesk.com", "teamviewer.com", "rustdesk.com", "airdroid.com", "supremocontrol.com",
)

PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


def is_blocked_package(pkg: str | None) -> bool:
    if not pkg:
        return False
    p = pkg.lower()
    return p in BLOCKED_PACKAGES or any(s in p for s in _BLOCKED_SUBSTRINGS)


def is_blocked_url(url: str | None) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(d in u for d in BLOCKED_DOMAINS)


def valid_package(pkg: str) -> bool:
    return bool(PACKAGE_RE.match(pkg or ""))


# --- Detekce podvodu z věty seniora ---------------------------------------------

def normalize(text: str) -> str:
    """Malá písmena, bez diakritiky, jedna mezera."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^a-z0-9@.:/ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Pozor: kontrola běží na normalizovaném textu (bez diakritiky).
_SCAM_PATTERNS = [
    r"\bbank\w*", r"\bbance\b", r"\bpolici\w*", r"\bpolicajt\w*",
    r"\banydesk\w*", r"\bteam ?viewer\w*", r"\brust ?desk\w*", r"\bvzdalen\w* pristup\w*",
    r"\bbezpecn\w* uc(et|tu)\b", r"\bheslo\b", r"\bhesl[ae]\b", r"\bpin\w*\b",
    r"\bkod\w* (z|ze|v) (sms|zprav\w*)", r"\boverovac\w* kod\w*", r"\bplatb\w*", r"\bzaplat\w*",
    r"\bprevod\w*", r"\bprevest\b", r"\bpenez\w*", r"\bpenize\b", r"\bkart[auy]\b", r"\bkartou\b",
    r"\bkrypto\w*", r"\bbitcoin\w*", r"\binvestic\w*", r"\bodinstal\w*", r"\bsmaz\w* (aplikac|vsechn)\w*",
    r"\brezim\w* pro vyvojare\b", r"\bvyvojar\w*",
    # podvodné SMS a e-maily: „zablokují vám účet, klikněte na odkaz“
    r"\bzablok\w* (\w+ ){0,2}(uc(et|tu)|kart\w*)", r"\bklik\w* na (\w+ )?odkaz\w*",
]
_SCAM_RE = re.compile("|".join(_SCAM_PATTERNS))


def looks_like_scam(text: str) -> bool:
    """Tvrdá kontrola v kódu – nezávislá na LLM (defense in depth)."""
    return bool(_SCAM_RE.search(normalize(text)))
