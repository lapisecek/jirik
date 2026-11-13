---
name: hledani-souboru
title: Najít ztracený soubor, fotku nebo dopis
description: Najít soubor, fotku, dopis nebo složku, o které senior neví, kde je, a otevřít ji.
keywords: najdi, najit, hledam, ztratil, ztratila, zmizel, zmizela, kde mam, kde je, nemuzu najit, soubor, dopis, dokument, fotk, obrazek, faktur, pdf, slozk
devices: pc
tools: find_files, list_files, open_item
first_tool: find_files
exclude: internet, web, google, seznam, youtub, wikiped, map
---
## Postup pro Aiko
1. Call find_files(name="<words from the senior>") - use 1-2 distinctive words, basic form, no diacritics needed
   ("dopis pro Janu" -> "dopis jan"). Add days=7 when the senior says "nedávno".
2. If nothing is found, try another word, or list_files(where="downloads", days=7) and list_files(where="desktop").
3. Several hits: pick the newest that fits; if two different files fit equally, ask_user with both names in Czech.
4. Open it: open_item(name="<file name>", where="<where from the result>"). Programs (.exe) are never opened.
5. done: say in Czech what you opened and where it is stored (e.g. "Dopis je v Dokumentech, teď ho máte otevřený").

## Pro seniora
1. Na klávesnici podržte klávesu s okénkem Windows a zmáčkněte písmeno E. Otevře se okno se složkami.
2. Vpravo nahoře je okénko s lupou „Hledat“. Klikněte do něj levým tlačítkem myši.
3. Napište jedno slovo z názvu souboru, třeba „dopis“, a chvilku počkejte.
4. Ve výsledcích dvakrát rychle klikněte na soubor, který hledáte. Otevře se.
