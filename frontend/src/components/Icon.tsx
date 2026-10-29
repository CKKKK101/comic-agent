import type { CSSProperties } from "react";

const paths = {
  home: "m3 10 9-7 9 7v10H3Z M9 20v-7h6v7",
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  users: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M22 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75",
  settings: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1Z",
  sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 2v2 M12 20v2 M2 12h2 M20 12h2 M5 5l1.5 1.5 M17.5 17.5 19 19 M5 19l1.5-1.5 M17.5 6.5 19 5",
  moon: "M21 13a9 9 0 0 1-10-10A9 9 0 1 0 21 13Z",
  plus: "M12 5v14 M5 12h14",
  arrow: "M4 12h16 M14 6l6 6-6 6",
  chevron: "m9 5 7 7-7 7",
  film: "M3 4h18v16H3Z M7 4v16 M17 4v16 M3 9h4 M3 15h4 M17 9h4 M17 15h4",
  sparkle: "m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z M20 2v4 M18 4h4",
  check: "m5 12 4 4L19 6",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2",
  close: "m6 6 12 12 M6 18 18 6",
  message: "M21 11a8 8 0 0 1-8 8H3l2-5a8 8 0 1 1 16-3Z M8 10h8 M8 14h5",
  image: "M3 3h18v18H3Z m0 14 6-6 5 5 3-3 4 4 M15 7h.01",
  layers: "m12 3 10 5-10 5L2 8Z M2 12l10 5 10-5 M2 16l10 5 10-5",
  play: "m8 4 12 8-12 8Z",
  refresh: "M20 7v5h-5 M4 17v-5h5 M5.2 7a8 8 0 0 1 13.5-2L20 7 M4 17l1.3 2A8 8 0 0 0 19 17",
  stop: "M5 5h14v14H5Z",
  download: "M12 3v12 m-5-5 5 5 5-5 M4 17v4h16v-4",
  edit: "m15 4 5 5 M4 20l5-1L21 7a2 2 0 0 0-5-5L4 14Z",
  search: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 m5 12 6 6",
  folder: "M3 7V4h6l3 3h9v13H3Z",
  trash: "M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7",
  panel: "M3 4h18v16H3Z M15 4v16",
  book: "M3 4h7l2 2 2-2h7v16h-7l-2 2-2-2H3Z M12 6v16",
} as const;

export type IconName = keyof typeof paths;
export function Icon({ name, size = 18, className, style }: { name: IconName; size?: number; className?: string; style?: CSSProperties }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className} style={{ flexShrink: 0, ...style }}><path d={paths[name]} /></svg>;
}
