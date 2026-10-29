import { createContext, useContext, useEffect, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";

type Theme = "dark" | "light";
const ThemeContext = createContext({ theme: "dark" as Theme, toggle: () => {} });
export const useTheme = () => useContext(ThemeContext);
const links: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "创作台", icon: "sparkle" },
  { to: "/projects", label: "我的项目", icon: "grid" },
  { to: "/characters", label: "角色库", icon: "users" },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => {
    try { return localStorage.getItem("comic-studio-theme") === "light" ? "light" : "dark"; }
    catch { return "dark"; }
  });
  const location = useLocation();
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    try { localStorage.setItem("comic-studio-theme", theme); } catch { /* 偏好存储不可用时保留当前会话主题 */ }
  }, [theme]);
  return <ThemeContext.Provider value={{ theme, toggle: () => setTheme(v => v === "dark" ? "light" : "dark") }}>
    <div className="studio-shell">
      <aside className="studio-sidebar">
        <Link to="/" className="studio-brand" aria-label="Comic Studio 首页"><span className="brand-mark"><Icon name="film" size={23} /></span><span>COMIC<span className="brand-sub">STUDIO / 漫剧创作</span></span></Link>
        <div className="workspace-switch"><span className="workspace-avatar">C</span><span><strong>个人工作空间</strong><small>你的故事，从这里开始</small></span><Icon name="chevron" size={13} /></div>
        <span className="nav-caption">创作空间</span>
        <nav className="studio-navigation" aria-label="主导航">
          {links.map(link => <NavLink key={link.to} to={link.to} end={link.to === "/"} className={({ isActive }) => `studio-nav-item${isActive || (link.to === "/projects" && location.pathname.startsWith("/project/")) ? " is-active" : ""}`}><Icon name={link.icon} /><span>{link.label}</span>{link.to === "/" && <span className="nav-shortcut">＋</span>}</NavLink>)}
        </nav>
        <div className="sidebar-note"><Icon name="layers" size={25} /><p>把脑海中的画面，<br />变成看得见的故事。</p><span>IDEA → STORY → FILM</span></div>
        <div className="sidebar-footer"><NavLink to="/settings" className={({ isActive }) => `studio-nav-item${isActive ? " is-active" : ""}`}><Icon name="settings" /><span>服务设置</span></NavLink><div className="sidebar-profile"><span className="profile-avatar">创</span><div><strong>独立创作者</strong><small>专注每一个好故事</small></div><span className="online-dot" /></div></div>
      </aside>
      <div className="studio-main">{children}</div>
      <nav className="mobile-navigation" aria-label="移动导航">{links.map(link => <NavLink key={link.to} to={link.to} end={link.to === "/"} className={({ isActive }) => isActive ? "is-active" : ""}><Icon name={link.icon} /><span>{link.label}</span></NavLink>)}<NavLink to="/settings"><Icon name="settings" /><span>设置</span></NavLink></nav>
    </div>
  </ThemeContext.Provider>;
}
