---
name: pomaly-pocitac
title: Počítač je pomalý, zasekává se nebo je plný
description: Počítač je pomalý, zasekává se, nic nejde, plný disk, moc otevřených oken, docházející baterie.
keywords: pomaly, pomalu, zasek, sekne, nejde nic, nereaguje, zamrz, plny disk, misto na disku, baterie, nabit, vybit, horky, hucí, huci, vetrak
devices: pc
tools: pc_health, focus_window
first_tool: pc_health
---
## Postup pro Aiko
1. Call pc_health. Read disk_free_percent, memory_used_percent, battery_percent, running_days, open_windows, hints.
2. Many open windows or memory > 90 %: offer to close windows the senior does not need - list them (from
   "Open windows") and ask_user which ones; close only those with focus_window + key alt+f4. Never close
   unsaved documents without asking.
3. Disk almost full: do NOT delete anything. Tell the senior and suggest asking the family.
4. Running more than 7 days: advise the senior to restart the computer themselves (Start -> Napájení ->
   Restartovat) - Aiko never restarts or shuts down the computer.
5. Battery low and not charging: tell the senior to plug in the charger.
6. done: a short Czech summary of what you found and what helped.

## Pro seniora
1. Zavřete okna, která nepotřebujete: vpravo nahoře v okně klikněte na křížek.
2. Když je počítač pořád pomalý, klikněte vlevo dole na Start, pak na ikonu vypínače a vyberte „Restartovat“.
3. Počkejte, až se počítač znovu zapne. Často to pomůže.
4. Když svítí, že je baterie slabá, připojte nabíječku.
