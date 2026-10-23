export const AVATAR_COLORS = [
  "#6d7cff", "#34d399", "#fbbf24", "#60a5fa",
  "#f472b6", "#2dd4bf", "#fb923c", "#a78bfa",
];

export function avatarColor(slug: string): string {
  let h = 0;
  for (const c of slug) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}
