# macos-background-cua-skill

A skill for Claude Code, Cursor CLI, or any other agent platform that enables computer use for background apps — similar to Sky Autopilot and OpenAI Codex Computer Use. That is, unlike regular "computer use" functionality, with this skill agents can drive apps on your computer in the background, without interfering with your ability to work on different apps in the foreground. 

It's based on some code from the [OpenQwaq](https://en.wikipedia.org/wiki/OpenQwaq) project in 2011 (Alan Kay, et al), and some experiments I made in 2020: [antimatter15/microtask](https://github.com/antimatter15/microtask/blob/master/cocoa/test15.py), with some very-2026 enrichment by Claude Opus 3.7.

It uses a mix of relatively obscure macOS APIs and Accessiblity APIs, so it doesn't work with all apps. It seems to work with iMessage (SwiftUI/AppKit), and the Claude Desktop App (Electron), and Day One, but it does not work with Bambu Studio (which uses Qt, wxWidgets).
