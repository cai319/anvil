import { useEffect, useState } from "react";
import { avatarColor } from "../avatarColor";
import { resolveAvatar } from "./avatarPresets";

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
  const [failed, setFailed] = useState(false);
  const resolved = resolveAvatar(avatar, slug);

  useEffect(() => {
    setFailed(false);
  }, [avatar]);

  if (resolved.kind === "image" && !failed) {
    return (
      <span
        className={`role-avatar role-avatar-image ${className}`}
        style={{ width: size, height: size }}
      >
        <img src={resolved.src} alt="" onError={() => setFailed(true)} />
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
    >
      {(name || slug).slice(0, 1).toUpperCase()}
    </span>
  );
}
