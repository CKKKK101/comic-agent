import { Link, useLocation } from "react-router-dom";
import { Icon } from "./Icon";
import { useTheme } from "./AppShell";

export function TopBar({ projectId }: { projectId?: number }) {
  const { pathname } = useLocation();
  const { theme, toggle } = useTheme();
  const title = projectId ? "项目工作台" : ({ "/": "创作台", "/projects": "我的项目", "/characters": "角色库", "/settings": "服务设置" }[pathname] ?? "创作空间");
  return <header className="studio-topbar">
    <div className="topbar-breadcrumb"><span className="desktop-label">工作空间</span><Icon name="chevron" size={12} /><strong>{title}</strong>{projectId != null && <span className="project-id">#{String(projectId).padStart(3, "0")}</span>}</div>
    <div className="topbar-actions"><span className="workspace-status"><span className="online-dot" />个人创作空间</span><button className="icon-button" onClick={toggle} aria-label={theme === "dark" ? "切换浅色主题" : "切换深色主题"} title={theme === "dark" ? "切换浅色主题" : "切换深色主题"}><Icon name={theme === "dark" ? "sun" : "moon"} /></button><Link to="/settings" className="icon-button" aria-label="设置" title="服务设置"><Icon name="settings" /></Link><span className="topbar-avatar">创</span></div>
  </header>;
}
