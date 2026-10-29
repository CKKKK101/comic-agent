import { LibraryBrowser } from "../components/CreativePanels";
import { TopBar } from "../components/TopBar";
import { Icon } from "../components/Icon";

export function CharactersPage() {
  return <div className="flex h-full flex-col">
    <TopBar />
    <main className="page-scroll"><div className="studio-page space-y-6">
      <div><p className="page-eyebrow"><Icon name="users" size={12} />YOUR CAST, YOUR UNIVERSE</p><h1 className="page-heading">角色库</h1><p className="page-description">保存形象与性格，让同一张熟悉的面孔，走进不同的故事。可在项目工作台中加入角色。</p></div>
      <LibraryBrowser />
    </div></main>
  </div>;
}
