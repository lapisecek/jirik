---
name: tisk
title: Tisk dokumentu nebo fotky
description: Vytisknout dokument, dopis, fotku nebo stránku; zjistit, proč tiskárna netiskne (offline, výchozí tiskárna, HP Smart).
keywords: tisk, tiskn, vytiskni, vytisknout, tiskarn, hp smart, papir, netiskne, kopie, fotk, dopis, dokument, pdf
devices: pc
tools: list_printers, find_files, print_file, start_app
first_tool: list_printers
---
## Postup pro Aiko
1. Call list_printers. If the default printer is "offline" or there is no real printer (only PDF/XPS/OneNote,
   marked virtual), stop with done and tell the senior in Czech to switch the printer on / check the cable;
   do not print to a virtual printer unless the senior asked for a PDF.
2. A file: find it with find_files (or list_files) and call print_file(name, where).
3. Something open on the screen (a web page, a Word document): key ctrl+p opens the print window; check the
   printer name at the top, then click "Tisk" / "Tisknout" / "Print". Do not change paid options.
4. HP printers: start_app(name="HP Smart") shows printer status and ink; use it only to look,
   never click "Koupit", "HP+", "Instant Ink" or subscriptions.
5. done: tell the senior what was sent to which printer.

## Pro seniora
1. Zkontrolujte, že tiskárna svítí a je v ní papír.
2. V otevřeném dokumentu podržte klávesu Ctrl vlevo dole a zmáčkněte písmeno P. Otevře se okno pro tisk.
3. Nahoře zkontrolujte jméno tiskárny, ať je to ta vaše, ne „PDF“.
4. Klikněte na tlačítko „Tisk“ nebo „Tisknout“ levým tlačítkem myši.
5. Když tiskárna píše „offline“, vypněte ji a zapněte tlačítkem na tiskárně a zkuste to znovu.
