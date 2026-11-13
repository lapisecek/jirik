# Aiko – místní AI pomocnice s hlasem Jirka

Desktopová aplikace, která běží celá na tomto počítači: chat, místní model z Ollamy (Gemma 3 4B, celý
v grafické kartě), český hlas Jirka (Piper) a ovládání **jen tohoto počítače**. Server poslouchá pouze
na `127.0.0.1`, z jiných zařízení v síti se k Aiko nikdo nepřipojí.

## Instalace (jednou)
1. Nainstalujte [Python 3.11+](https://www.python.org), [Node.js](https://nodejs.org) a [Ollamu](https://ollama.com).
2. Dvojklik na **`Setup.bat`**. Nainstaluje knihovny, stáhne hlas Jirka a model a vytvoří na ploše ikonu **Aiko**.

## Spuštění
Dvojklik na ikonu **Aiko** na ploše. Aplikace sama spustí Ollamu, server Aiko i ovládání počítače
a po zavření okna je zase vypne.

Když Aiko něco dělá na počítači, kolem obrazovky se rozsvítí barevný rámeček a nahoře se objeví lišta
s tlačítkem **Stop**. Rámeček kliky propouští a na snímcích obrazovky, které Aiko čte, není vidět.
Okno s chatem se na dobu práce schová do lišty, aby do něj Aiko neklikala.

## Co umí
- Odpovídat na otázky, psát básničky a mluvit hlasem Jirka (tlačítko s reproduktorem vpravo nahoře).
- Ovládat počítač: otevřít web, pustit video na YouTube, vytvořit složku, najít soubor, zahrát Hledání min…
  Před každou změnou se zeptá. **Stop** v aplikaci nebo v liště vše zastaví.
- Nikdy nesahá do bankovnictví, hesel ani vzdáleného přístupu a nepíše kódy.

## Struktura
| Složka / soubor | Co to je |
|---|---|
| `app/` | aplikace Electron: `main.js` (spouští vše, okna, rámeček), `splash.html`, `overlay.html`, `pill.html`, ikona |
| `aiko/` | server Aiko (Python): porozumění, recepty, GUI agent, hlas; chat je `aiko/server/static/chat.html` |
| `aiko.toml` | nastavení: model, hlas, oslovení |
| `Setup.bat`, `setup.ps1` | instalace všeho + ikona na ploše (`tools/create-shortcut.ps1`) |
| `Start Aiko.bat` | záložní spuštění bez aplikace – chat v prohlížeči |
| `data/logs/` | záznamy aplikace, serveru a ovládání počítače (při potížích) |

## Nastavení (`aiko.toml`)
- Model: `[llm] ollama_model` a `[vlm] ollama_model` (model musí umět obrázky). Chytřejší, ale pomalejší: `gemma4:latest`.
- `keywords_first = true`: jasné věty se vyřídí hned bez modelu (rychlejší odpovědi).
- Rychlost řeči: `[voice] speech_rate`. Oslovení: `[aiko] senior_name`.
