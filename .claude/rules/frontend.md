---
glob: "frontend/**/*.tsx"
---

# Frontend Rules

- All API calls go through the API_BASE constant, never hardcode URLs
- Use TypeScript interfaces for all data shapes
- Inline styles only (no CSS files) — this project uses inline style objects
- State updates must not cause race conditions — prefer override params over setTimeout
- All new inbox/email state must update both the list and totalEmails count
- Error states must set both errorMsg and status='error'
