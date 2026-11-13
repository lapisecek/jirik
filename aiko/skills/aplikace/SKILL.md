---
name: aplikace
title: Otevřít, přepnout a zavřít aplikaci
description: Spustit program nebo aplikaci (Word, Fotky, Kalkulačka, HP Smart, Chrome…), přepnout mezi okny, zavřít okno.
keywords: otevri, otevrit, spust, spustit, zapni, aplikac, program, word, excel, kalkulack, fotky, malovani, poznamkovy blok, chrome, edge, firefox, hp smart, skype, prepni, zavri, okno
devices: pc
tools: start_app, focus_window
first_tool: start_app
---
## Postup pro Aiko
1. Start an app: tool start_app(name="<name as the senior says it>") - Czech or English names both work.
   If it returns started=false with candidates, call start_app again with the best candidate; if none fits,
   tell the senior it is not installed (never download or install anything).
2. Switch to an already open window: focus_window(title="<part of the window title>") - see "Open windows".
3. Close the window in front: key alt+f4 ONLY when that window is the one the senior wants closed and it is
   not the desktop. Never close windows the senior did not mention.
4. Check the screenshot that the app is in front, then done with a short Czech sentence.

## Pro seniora
1. Vlevo dole nebo dole uprostřed klikněte na tlačítko s okénkem Windows (Start).
2. Hned začněte psát jméno programu, třeba „Word“ nebo „Kalkulačka“.
3. Nahoře v nabídce se objeví program. Klikněte na něj jednou levým tlačítkem myši.
4. Mezi otevřenými okny přepnete kliknutím na jejich ikonu dole na liště.
