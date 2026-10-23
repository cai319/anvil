export const AVATAR_PRESETS = [
  "ace", "bee", "cid", "dot", "eli",
  "fay", "gus", "hana", "ivan", "june",
  "kip", "luna", "max", "nia", "omar",
] as const;

const PRESET_SET = new Set<string>(AVATAR_PRESETS);

/** Stable mapping: same slug always gets the same default avatar. */
export function defaultAvatarFor(slug: string): string {
  let h = 2166136261;
  for (const ch of slug) {
    h ^= ch.charCodeAt(0);
    h = Math.imul(h, 16777619);
  }
  const index = (h >>> 0) % AVATAR_PRESETS.length;
  return `big-smile:${AVATAR_PRESETS[index]}`;
}

export type ResolvedAvatar =
  | { kind: "image"; src: string }
  | { kind: "initials" };

export function resolveAvatar(
  value: string | null | undefined,
  slug: string,
): ResolvedAvatar {
  if (value === "initials") return { kind: "initials" };

  const preset = value?.match(/^big-smile:([a-z0-9-]+)$/)?.[1];
  const id = preset && PRESET_SET.has(preset)
    ? preset
    : defaultAvatarFor(slug).slice("big-smile:".length);

  return { kind: "image", src: `/avatars/big-smile/${id}.svg` };
}
