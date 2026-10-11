import { CheckIcon } from "@/components/icons";
import { AGENT_SKINS, type AgentSkin } from "./skins";
import type { Theme } from "./use-theme";

export function AppearanceSettings({ theme, skin, onThemeChange, onSkinChange, hidden }: Readonly<{
  theme: Theme; skin: AgentSkin; onThemeChange: (theme: Theme) => void;
  onSkinChange: (skin: AgentSkin) => void; hidden: boolean;
}>) {
  return (
    <section className="settings-view appearance-view" hidden={hidden}>
      <h3>Make Talos feel like yours</h3>
      <div className="theme-switcher" role="group" aria-label="Color mode">
        {(["light", "dark"] as const).map((mode) => <button key={mode} type="button" aria-pressed={theme === mode} onClick={() => onThemeChange(mode)}><span>{mode === "light" ? "Light" : "Dark"}</span>{theme === mode && <span className="skin-selected"><CheckIcon /><span className="sr-only">Selected</span></span>}</button>)}
      </div>
      <h3 className="skin-heading">Agent skin</h3>
      <div className="skin-switcher" role="group" aria-label="Agent skin">
        {AGENT_SKINS.map((item) => <button key={item.id} type="button" aria-pressed={skin === item.id} onClick={() => onSkinChange(item.id)}><i aria-hidden="true" style={{ backgroundColor: item[theme] }} /><span><strong>{item.name}</strong></span>{skin === item.id && <span className="skin-selected"><CheckIcon /><span className="sr-only">Selected</span></span>}</button>)}
      </div>
    </section>
  );
}
