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
  /** 角色/会话状态；由 CSS 映射为头像阴影颜色。 */
  status?: string;
  /** 无可见姓名时提供可访问名称（角色名 + 状态）。 */
  label?: string;
};

export function RoleAvatar({
  slug,
  name,
  avatar,
  color,
  size = 28,
  className = "",
  status,
  label,
}: Props) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const resolved = resolveAvatar(avatar, slug);
  const failed = resolved.kind === "image" && failedSrc === resolved.src;
  const sizing = {
    width: `var(--avatar-size, ${size}px)`,
    height: `var(--avatar-size, ${size}px)`,
  };
  const a11y = {
    "aria-hidden": label ? undefined : true,
    "aria-label": label,
    role: label ? "img" : undefined,
    title: label,
  } as const;

  if (resolved.kind === "image" && !failed) {
    return (
      <span
        className={`role-avatar role-avatar-image ${className}`}
        style={sizing}
        data-role-status={status}
        {...a11y}
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
        ...sizing,
        background: color || avatarColor(slug),
        fontSize: `var(--avatar-font, ${Math.max(10, Math.round(size * 0.4))}px)`,
      }}
      data-role-status={status}
      {...a11y}
    >
      {initialFor(name, slug)}
    </span>
  );
}
