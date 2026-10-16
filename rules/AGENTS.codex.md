# Multi-Pool Orchestration & Quota Rules (Codex / Antigravity / Claude)
- Use `/home/u24c9g5090r64/.local/bin/maestro worker-start --pool auto --difficulty <high|medium|low> --cwd <dir> --wait --spec "<task>"` for cross-pool subagents (auto-steers away from `0%` pools; `7d<=10%` conserve, `7d<=2%` reserve).
- Default Codex speed tier to `priority` (`Fast` ⚡); use `--urgent` (`ultrafast` ⚡⚡⚡) only when urgent.
- Always state task ETAs in **AI parallel wall-clock time (`⏱ 30초~1분` / `⏱ 1~3분` / `⏱ 3~6분` / `⏱ 7~12분`) + Korean token count with KRW & 5h/7d quota % (`8천~1만5천 토큰 (5백원대 · 5h 0.3%)` / `10만~15만 토큰 (5천원대 · 5h 2~4% / 7d 0.5%)` / `100만~150만 토큰 (5만원대 · 5h 20~30% / 7d 3~5%)` / `1천만 토큰 (53만원대 · 7d 15~25% 분산)`)**, never human days/weeks or "약".
