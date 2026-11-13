<p align="center"><img src="app/assets/jirik.png" width="112" alt="Jiřík"></p>

<h1 align="center">Jiřík</h1>
<p align="center"><b>Místní AI asistent, který ovládá tvůj počítač – i ostatní počítače v síti.</b><br>
Běží celý u tebe: model, hlas i ovládání. Nic neodchází do cloudu.</p>

---

## Co Jiřík umí

- **Chatovat** jako běžný chatbot – odpovědi proudí živě, umí tabulky, kód, vložené obrázky (Ctrl+V / přetažení).
- **Přemýšlet** – u vyšších úrovní ukazuje své uvažování živě (rozbalovací blok „Přemýšlím…“).
- **Ovládat počítač jako člověk** – vidí obrazovku (snímek s očíslovanými prvky a jejich souřadnicemi), hýbe
  myší, kliká, přetahuje, píše, používá zkratky a po každém kroku si ověří nový snímek. Pracuje tak dlouho,
  dokud úkol na obrazovce opravdu není hotový.
- **Rovnou dělat věci** – PowerShell (soubory, nastavení, procesy, instalace přes winget), otevírání aplikací,
  webů a souborů, hledání a čtení webu, počasí, YouTube. Bez otázek „mám to udělat?“.
- **Mluvit** českým hlasem (Piper · Jirka). Hlas přehrává jen jedno místo, takže i s více otevřenými okny
  zní každá věta právě jednou.
- **Ovládat další počítače** v záložce **Zařízení** – přidání zabere minutu, spící počítač se sám probudí.

## Úrovně myšlení

Vybírají se v okně zprávy (tlačítko s pruhy). Jiřík u každé úrovně ukáže, jestli se vejde do grafiky tvého
počítače, jak je rychlá (po stažení naměřeno) a kolikrát je pomalejší než Nízká. Chybějící úroveň se stáhne
jedním kliknutím.

| Úroveň | Model | Stažení | Pro co |
|---|---|---|---|
| **Nízká** | `qwen3.5:4b` | 3,4 GB | okamžité odpovědi, běžné úkoly |
| **Střední** | `qwen3.5:9b` | 6,6 GB | chytřejší odpovědi, spolehlivější klikání |
| **Chytrá** | `qwen3.5:9b` + přemýšlení | (stejný) | plánování, složitější úkoly |
| **Ultra** | `qwen3.5:27b` + přemýšlení | 17 GB | nejlepší výsledky, potřebuje silný počítač |

Proč Qwen3.5: podle srovnání pro 6–16 GB grafiky je to nejlepší místní rodina modelů – každá velikost umí text,
obrázky, nástroje i přemýšlení a všechny používají stejné souřadnice obrazovky, takže ovládání myší funguje
stejně na každé úrovni. U úrovní s přemýšlením Jiřík přemýšlí při plánování a kroky na obrazovce pak
provádí rychle. **Necenzurovaný režim** (přepínač v nabídce i v Nastavení) přepne na abliterated verze
stejných modelů (`huihui_ai/qwen3.5-abliterated`).

Na grafice se 6 GB (např. RTX 3050) běží Nízká celá v grafice (~45 tokenů/s), Střední a Chytrá částečně
v RAM (~9 tokenů/s). Ultra je na takový počítač příliš velká.

## Instalace (od nuly)

1. Stáhni si [poslední verzi](../../releases/latest) a rozbal ji.
2. Dvojklik na **`Setup.bat`**. Samo doinstaluje, co chybí – Python, Node.js, Ollamu (přes winget), knihovny,
   hlas, model pro úroveň Nízká – a vytvoří na ploše ikonu **Jiřík**.
3. Spusť Jiříka ikonou na ploše. Když instalace ještě neproběhla, aplikace ji při prvním spuštění spustí sama.

Požadavky: Windows 10/11, 16 GB RAM doporučeno, grafika NVIDIA výhodou (bez ní to jde, jen pomaleji),
asi 15 GB volného místa. Záložní spuštění bez aplikace (chat v prohlížeči): `Start Jirik.bat`.
Jméno a oslovení si nastavíš v `aiko.user.toml` (vzor: `[aiko]` `user_name = "Adam"` `greeting = "Adame"`).

## Další počítače (záložka Zařízení)

1. V Jiříkovi otevři **Zařízení → Přidat zařízení → Začít**.
2. Na druhém počítači (ve stejné síti) otevři v prohlížeči zobrazenou adresu a spusť stažený instalátor.
   Vše potřebné se doinstaluje samo.
3. Oba počítače ukážou **stejný šestimístný kód** – potvrď v Jiříkovi „Kódy se shodují“. Hotovo.

Pak stačí vybrat počítač vlevo a napsat, co na něm udělat. Náhled jeho obrazovky je pod seznamem.

**Jak to funguje:** na druhém počítači běží jen malý hlídač (~13 MB RAM, nulové CPU). Když ho Jiřík potřebuje,
pošle mu podepsanou zprávu a hlídač spustí plné ovládání, které se po 10 minutách bez práce zase ukončí.
Spící počítač Jiřík probudí přes **Wake-on-LAN** (instalátor ho na síťové kartě zapne). Během práce se počítač
neuspí ani nezhasne obrazovku.

## Zabezpečení

- **Párování bez důvěry v síť:** výměna klíčů X25519 a ověření šestimístným kódem (útočník uprostřed by měl
  jiný kód). Klíč zařízení se předá až po potvrzení, šifrovaný AES-256-GCM. Párovací odkaz platí 15 minut
  a jen jednou; instalační balíček neobsahuje žádná tajemství a instalátor ověřuje jeho SHA-256.
- **Spojení:** TLS 1.2+ s připnutým certifikátem Jiříka (podvržený server v síti nic neovládne) a vzájemné
  ověření HMAC-SHA256 klíčem zařízení. Probouzení je podepsané, s časovým razítkem a ochranou proti přehrání.
- **Klíče na disku** jsou šifrované Windows DPAPI (vázané na účet uživatele). Každé zařízení má vlastní 256bit
  klíč – odebráním v aplikaci okamžitě přestane platit.
- **Síť:** chat a API poslouchají jen na `127.0.0.1`. Do sítě je otevřená jen šifrovaná brána a probouzení,
  a to jen pro místní podsíť a soukromé sítě (pravidla firewallu).
- **Pojistky v kódu**, které model nepřebije: žádné formátování disků, mazání systémových složek, vypínání
  antiviru či firewallu, změny uživatelských účtů ani spouštění kódu staženého z internetu. Hesla a ověřovací
  kódy Jiřík nepíše; do bankovnictví, správců hesel a vzdáleného přístupu nesahá.

**Proč Jiřík neodemyká zamčený počítač:** odemknutí obrazovky na dálku by znamenalo uložit heslo k Windows
a vložit do přihlašovací obrazovky komponentu běžící jako SYSTEM – přesně to dělá malware a bezpečnostní
doporučení před tím varují. Jiřík místo toho počítač probudí, během práce ho nenechá usnout ani zamknout,
a když je zamčený, řekne to (příkazy přes PowerShell fungují i tak).

## Řešení potíží

| Potíž | Řešení |
|---|---|
| „Ollama neběží“ | spusť aplikaci Ollama, nebo znovu `Setup.bat` |
| „Model chybí“ | v nabídce Myšlení klikni u úrovně na stažení |
| Zařízení je „Nedostupné“ | je zapnuté, přihlášené a ve stejné síti? Síť musí být „Soukromá“ |
| Pomalé odpovědi | nižší úroveň myšlení; zavři hry a programy, které zabírají grafiku |
| Záznamy | `data/logs/` (aplikace, server, ovládání, instalace) |

## Pro vývojáře

| Cesta | Co to je |
|---|---|
| `app/` | Electron: okno, spuštění všeho, rámeček a lišta při práci, ikona (`make_icon.js` + `make_icon.py`) |
| `aiko/server/chat.py` | konverzace: úrovně, přemýšlení, nástroje, ovládání obrazovky, dva prostory |
| `aiko/server/hardware.py` | úrovně myšlení, odhad toho, co počítač zvládne |
| `aiko/server/link.py` | Jiřík Link: párování, TLS brána, probouzení, Wake-on-LAN |
| `aiko/agents/pc.py` | ovládání počítače (lokálně i na spárovaných počítačích) |
| `aiko/link/` | hlídač, párovací klient a instalátor pro druhý počítač |
| `aiko.toml` / `aiko.user.toml` | nastavení / osobní nastavení (jméno, oslovení – git ho ignoruje) |

Licence: MIT.
