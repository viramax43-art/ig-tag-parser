/** Разбивает dump-аккаунты (user:pass:2FA|Instagram …) на строки, даже если вставлены слитно. */

const DUMP_ACCOUNT =
  /([a-z][a-z0-9._]{2,29}):([^:\s|]{3,64}):([A-Z2-7]{16,64})\|Instagram\s[\s\S]*?(?=([a-z][a-z0-9._]{2,29}:[^:\s|]{3,64}:[A-Z2-7]{16,64}\|Instagram\s)|$)/g;

export function normalizeAccountsText(raw: string): string {
  const text = raw.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (!text.trim()) return "";

  const chunks: string[] = [];
  DUMP_ACCOUNT.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = DUMP_ACCOUNT.exec(text)) !== null) {
    const chunk = m[0].trim();
    if (chunk) chunks.push(chunk);
    if (m[0].length === 0) DUMP_ACCOUNT.lastIndex++;
  }

  if (chunks.length >= 1) {
    const firstIdx = text.search(
      /[a-z][a-z0-9._]{2,29}:[^:\s|]{3,64}:[A-Z2-7]{16,64}\|Instagram\s/,
    );
    const prefix =
      firstIdx > 0
        ? text
            .slice(0, firstIdx)
            .split("\n")
            .map((l) => l.trim())
            .filter((l) => l.startsWith("#") || l.startsWith("//"))
        : [];
    return [...prefix, ...chunks].join("\n") + "\n";
  }

  const lines = text
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  return lines.length ? lines.join("\n") + "\n" : "";
}

export function countAccountLines(text: string): number {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("#") && !l.startsWith("//")).length;
}
