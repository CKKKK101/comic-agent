/** 与后端 /api/v1 契约对齐的类型(单机 demo 精简版) */

export interface Character {
  name: string;
  personality: string;
  appearance: string;
  quirks?: string;
  library_id?: number;
  reference_url?: string | null;
}

export interface LibraryCharacter extends Character {
  id: number;
  style: string;
  image_url: string | null;
  created_at: string;
}

export interface ReferenceImage {
  id: string;
  kind: "style" | "character" | "shot";
  target_index: number | null;
  url: string;
}

export interface Shot {
  index: number;
  scene: string;
  camera: string;
  action: string;
  dialogue: string;
  duration?: number;
  character_ids: number[];
  image_prompt?: string;
  video_prompt?: string;
}

export interface CharacterImage {
  character_name: string;
  url: string;
  prompt?: string;
}

export interface ShotImage {
  shot_index: number;
  url: string;
  prompt?: string;
  stale?: boolean;
}

export interface OutlineAct {
  title: string;
  plot: string;
}

export interface Outline {
  title?: string;
  logline?: string;
  genre?: string[];
  themes?: string[];
  setting?: string;
  tone?: string;
  style_note?: string;
  acts?: OutlineAct[];
}

export interface VideoMeta {
  url: string;
  voiceover_url?: string | null;
  notes?: string;
}

export interface ProjectContent {
  outline?: Outline;
  characters?: Character[];
  shots?: Shot[];
  character_images?: CharacterImage[];
  shot_images?: ShotImage[];
  video?: VideoMeta;
  reference_images?: ReferenceImage[];
  cast?: Character[];
  video_dirty?: boolean;
}

export interface RunView {
  id: number;
  status: string; // running|succeeded|failed|cancelled|interrupted
  current_stage: string | null;
  progress: number;
  auto_mode: boolean;
  video_pending: boolean;
  error: string | null;
}

export interface MessageItem {
  id: number;
  agent: string;
  role: string; // user|assistant
  content: string;
}

export interface ProjectActions {
  can_generate: boolean;
  can_resume: boolean;
  can_cancel: boolean;
  waiting_gate: string | null;
  can_complete_video: boolean;
  video_blocker: string | null;
  can_edit_shots: boolean;
  can_manage_cast: boolean;
  can_manage_references: boolean;
  edit_blocker: string | null;
}

export interface ProjectView {
  id: number;
  title: string;
  topic: string;
  style: string;
  status: string; // draft|generating|succeeded|failed
  content: ProjectContent;
  created_at: string;
  latest_run: RunView | null;
  messages: MessageItem[];
  actions: ProjectActions;
}

export interface ProjectSummary {
  id: number;
  topic: string;
  style: string;
}

export interface ConfigItemView {
  is_sensitive: boolean;
  is_set: boolean;
  value: string;
  current: string;
}

export interface ConfigResponse {
  items: Record<string, ConfigItemView>;
}

export const STAGE_LABEL: Record<string, string> = {
  plan_outline: "故事大纲",
  outline_approval: "大纲确认",
  plan_characters: "角色设计",
  characters_approval: "角色确认",
  plan_shots: "分镜脚本",
  shots_approval: "分镜确认",
  render_characters: "角色立绘",
  character_images_approval: "立绘确认",
  critique_character_images: "立绘质检",
  render_shots: "分镜画面",
  shot_images_approval: "画面确认",
  critique_shot_images: "画面质检",
  compose: "成片合成",
  compose_approval: "成片确认",
  review: "反馈修订",
};

/** 三段阶段轨道:规划/渲染/成片 */
export const PIPELINE: { key: string; label: string }[] = [
  { key: "plan", label: "规划" },
  { key: "render", label: "渲染" },
  { key: "compose", label: "成片" },
];

const PHASE_OF_STAGE: Record<string, string> = {
  plan_outline: "plan",
  outline_approval: "plan",
  plan_characters: "plan",
  characters_approval: "plan",
  plan_shots: "plan",
  shots_approval: "plan",
  render_characters: "render",
  character_images_approval: "render",
  critique_character_images: "render",
  render_shots: "render",
  shot_images_approval: "render",
  critique_shot_images: "render",
  compose: "compose",
  compose_approval: "compose",
  review: "plan",
};

export function phaseOfStage(stage: string | null | undefined): string | null {
  if (!stage) return null;
  return PHASE_OF_STAGE[stage] ?? null;
}

/** fake:// 开头的占位产物(离线 provider) */
export function isPlaceholder(url: string | null | undefined): boolean {
  return !url || url.startsWith("fake://");
}

export function stageIsApproval(stage: string | null | undefined): boolean {
  return Boolean(stage && stage.endsWith("_approval"));
}
