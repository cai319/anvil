import { useState } from "react";
import { avatarColor } from "../avatarColor";
import { initialFor, resolveAvatar } from "./avatarPresets";

type Props = {
  slug: string;
  name: string;
  avatar?: string | null;
  color?: string | null;
  size?: number;
  className?: string;
};

export function RoleAvatar({
  slug,
  name,
  avatar,
  color,
  size = 28,
  className = "",
}: Props) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const resolved = resolveAvatar(avatar, slug);
  const failed = resolved.kind === "image" && failedSrc === resolved.src;

  if (resolved.kind === "image" && !failed) {
    return (
      <span
        className={`role-avatar role-avatar-image ${className}`}
        style={{ width: size, height: size }}
        aria-hidden="true"
      >
        <img
          src={resolved.src}
          alt=""
          style={{ width: "100%", height: "100%", objectFit: "cover", borderRadius: "50%" }}
          onError={() => setFailedSrc(resolved.src)}
        />
      </span>
    );
  }

  return (
    <span
      className={`role-avatar ${className}`}
      style={{
        width: size,
        height: size,
        background: color || avatarColor(slug),
        fontSize: Math.max(10, Math.round(size * 0.4)),
      }}
      aria-hidden="true"
    >
      {initialFor(name, slug)}
    </span>
  );
}
