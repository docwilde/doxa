# Antigravity in DOXA

The native Linux preview runs Google's Antigravity CLI over ACP in a standalone DOXA tab. Install a version with ACP and choose your personal Google account during sign-in. [Google Antigravity CLI setup](https://antigravity.google/docs/cli/acp).

```sh
agy acp --login
doxa doctor --engine antigravity --model "Gemini 4 Neon"
doxa new --engine antigravity --model "Gemini 4 Neon"
```

`doctor` reads the active CLI's advertised catalog without submitting a prompt. An explicit model must match an advertised ID or a unique display name. For a default model, omit `--model`. In the TUI, select **Antigravity** from **New Session** and choose an advertised model. `--antigravity-bin PATH` or `DOXA_ANTIGRAVITY_BIN` selects a specific CLI executable. `--effort` accepts choices the CLI advertises for the session.

DOXA starts `agy acp` with its terminal sandbox enabled. ACP permission requests with a complete reviewable operation offer one-time Allow or Deny in DOXA. Antigravity applies its own workspace and account permission rules before requesting approval, including any existing allow rules. Keep a review-based permission preset in Antigravity. Google controls quota, credit settings, and charges. DOXA dollar ceilings, fleet or Router execution, Docker, and image input are outside this preview.

Detach and reattach as with other DOXA sessions. After a clean turn and shutdown, pick the conversation from history or use `doxa new --engine antigravity --resume FULL_DOXA_SESSION_ID`. DOXA asks Antigravity to resume the saved ACP session and restores its own conversation from the local transcript.

## LORE

DOXA sends a bounded LORE snapshot for the working directory with the first turn of each attached Antigravity process. It keeps that snapshot out of the DOXA conversation transcript. DOXA scrubs prompts and streamed replies and stores completed turns in the same owner-private transcript format as other engines. LORE indexes the transcript when the session stops. `DOXA_LORE=0` skips memory injection and indexing. Secret scrubbing remains required. Antigravity itself owns any session data or logs it writes.
