---
name: hledani-min
title: Hledání min (Minesweeper) – zahrát si
description: Otevřít oficiální hru Google Minesweeper (Hledání min) na počítači a zahrát ji.
keywords: minesweeper, hledani min, miny, minolovk, hra miny, zahraj miny
devices: pc
tools: play_minesweeper
first_tool: play_minesweeper
---
## Postup pro Aiko
1. Do NOT click the game cells yourself and do NOT open any other minesweeper website or a copy of the game.
   Call tool play_minesweeper {"max_seconds": 90}. It opens the official Google Minesweeper
   (https://www.google.com/fbx?fbx=minesweeper), reads the board from the screen and plays it safely.
2. Result status: "won" or "lost" -> done, tell the senior in Czech (the "say" field is ready).
   "time_up" -> call play_minesweeper again with open_game=false.
   "no_board" -> look at the screen: decline cookies with click_text "Odmítnout vše", close what covers the
   game, then call play_minesweeper again. "stopped_by_user" -> done: the senior took over the mouse.

## Pro seniora
1. Hra je na obrazovce: zelená políčka jsou zakrytá, pod některými jsou miny.
2. Klikněte levým tlačítkem myši na zelené políčko. Číslo říká, kolik min je kolem.
3. Kde je jistě mina, klikněte pravým tlačítkem myši – objeví se vlaječka.
4. Vyhrajete, když odkryjete všechna políčka bez min.
