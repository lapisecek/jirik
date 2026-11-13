---
name: stazene-soubory
title: Stažené soubory – najít a přesunout do složky
description: Najít dnes nebo nedávno stažené soubory a přesunout je do složky na ploše nebo v Dokumentech.
keywords: stazen, stahl, stahla, stahnul, download, prilohy, priloha, presun, presunout, dej tam, uloz, uklid, slozk
devices: pc
tools: list_files, create_folder, move_files, open_item
first_tool: list_files
---
## Postup pro Aiko
Use the file tools - never drag files with the mouse.
1. If the senior wants a new folder, call create_folder(name, where="desktop") first (or "documents").
2. Call list_files(where="downloads", days=1) for "dnes"/"today" (days=2 for "včera", days=7 for "tento týden").
   The result lists names, kinds and times; "dnes HH:MM" means today.
3. Choose the files that match the request (all of today's, or only photos -> kind "obrázek", PDFs…).
   Never move items marked "program" (installers) - tell the senior they stay in Downloads.
4. Call move_files(names_json='["a.pdf","b.jpg"]', source="downloads", target="desktop/<folder>").
   Use the exact names from list_files. Nothing is overwritten.
5. Call open_item(where="desktop/<folder>") so the senior sees the result.
6. done: say in Czech how many files you moved and where (e.g. "Na ploše ve složce pejsek jsou 3 soubory").

## Pro seniora
1. Na klávesnici podržte klávesu s okénkem Windows (dole vlevo) a k tomu zmáčkněte písmeno E. Otevře se okno se složkami.
2. V levém sloupci klikněte na „Stažené soubory“.
3. Úplně nahoře je skupina „Dnes“. To jsou dnešní stažené soubory.
4. Klikněte na první z nich. Pak podržte klávesu Shift (šipka nahoru vlevo dole) a klikněte na poslední. Všechny zmodrají.
5. Na kterýkoli modrý soubor klikněte pravým tlačítkem myši a vyberte „Vyjmout“ (obrázek nůžek).
6. V levém sloupci klikněte na „Plocha“ a dvakrát rychle klikněte na svou složku.
7. Do prázdného bílého místa klikněte pravým tlačítkem myši a vyberte „Vložit“.
