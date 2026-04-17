# Agent Skill for Background App Computer Use on macOS

This skill allows Claude, Cursor, or any other agent to operate apps in the background on a Mac. Like in Sky Autopilot and Codex CUA, with this skill, agents can drive apps on your computer in the background, without interfering with your ability to multitask and use your computer simultaneously. 

It's based on some code from the [OpenQwaq](https://en.wikipedia.org/wiki/OpenQwaq) project in 2011 (Alan Kay, et al), and some experiments I made in 2020: [antimatter15/microtask](https://github.com/antimatter15/microtask/blob/master/cocoa/test15.py), with some very-2026 enrichment by Claude Opus 3.7.

It uses a mix of relatively obscure macOS APIs and Accessiblity APIs, so it doesn't work with all apps. It seems to work with iMessage (SwiftUI/AppKit), and the Claude Desktop App (Electron), and Day One, but it does not work with Bambu Studio (which uses Qt, wxWidgets).
