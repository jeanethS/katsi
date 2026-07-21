import { useEffect, useState } from "react";
import { BookOpen, ChatCircleText, Cloud, Gear, Moon, Sun } from "@phosphor-icons/react";
import { Button, Card, ProgressBar, SourceChip } from "./components/primitives";
import { useT } from "./i18n/useT";
import { useUiStore } from "./stores/ui";

type Route = "library" | "ask" | "status" | "dev";
type Mode = "auto" | "local" | "cloud";

const modeKeys: Record<Mode, "ask.auto" | "ask.local" | "ask.cloud"> = {
  auto: "ask.auto",
  local: "ask.local",
  cloud: "ask.cloud",
};

const routes: Record<Exclude<Route, "dev">, { icon: typeof BookOpen }> = {
  library: { icon: BookOpen },
  ask: { icon: ChatCircleText },
  status: { icon: Gear },
};

function currentRoute(): Route {
  const route = window.location.hash.replace("#", "");
  return route === "library" || route === "ask" || route === "status" || route === "dev" ? route : "library";
}

function Receipt({ mode, t }: { mode: Mode; t: ReturnType<typeof useT> }) {
  const cloud = mode === "cloud";
  const rows = [
    [t("receipt.mode"), cloud ? t("ask.cloud") : mode === "local" ? t("ask.local") : t("ask.auto")],
    [t("receipt.files"), "4"],
    [t("receipt.chunks"), "6"],
    [t("receipt.localTokens"), "1,842"],
    [t("receipt.cloudTokens"), cloud ? "936" : "0"],
    [t("receipt.time"), "3.2s"],
  ];
  return (
    <details className="receipt" open>
      <summary>{t("receipt.title")}</summary>
      <dl>
        {rows.map(([label, value]) => (
          <div className="receipt-row" key={label}>
            <dt>{label}</dt>
            <span aria-hidden="true" className="leaders" />
            <dd className={label === t("receipt.cloudTokens") && cloud ? "semantic-cloud" : ""}>
              {value}
              {label === t("receipt.mode") && <span className={cloud ? "semantic-cloud" : "semantic-local"}> ●</span>}
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function Library({ t }: { t: ReturnType<typeof useT> }) {
  const [hasFolder, setHasFolder] = useState(true);
  return (
    <section className="screen">
      <header className="screen-header">
        <h1 className="display screen-title">{t("library.title")}</h1>
        <Button onClick={() => setHasFolder(true)}>{t("library.addFolder")}</Button>
      </header>
      {hasFolder ? (
        <div className="folder-list">
          <Card>
            <div className="folder-row">
              <div>
                <p className="folder-name">{t("folder.name")}</p>
                <p className="path">{t("folder.path")}</p>
              </div>
              <Button variant="ghost">•••</Button>
            </div>
            <p className="metadata"><span className="status-dot" />{t("folder.meta")}</p>
          </Card>
          <Card>
            <div className="folder-row">
              <div>
                <p className="folder-name">{t("folder.research")}</p>
                <p className="path">{t("folder.researchPath")}</p>
              </div>
              <Button>{t("library.index")}</Button>
            </div>
          </Card>
          <Card>
            <div className="folder-row">
              <div>
                <p className="folder-name">{t("folder.projects")}</p>
                <p className="path">{t("folder.projectsPath")}</p>
              </div>
              <Button variant="ghost">{t("folder.cancel")}</Button>
            </div>
            <p className="metadata">118 / 412 / china-trip.md</p>
            <ProgressBar value={29} />
          </Card>
        </div>
      ) : (
        <div className="empty-state">
          <h2 className="display">{t("library.emptyTitle")}</h2>
          <p>{t("library.emptyBody")}</p>
          <Button onClick={() => setHasFolder(true)}>{t("library.addFolder")}</Button>
        </div>
      )}
    </section>
  );
}

function Ask({ t }: { t: ReturnType<typeof useT> }) {
  const [mode, setMode] = useState<Mode>("local");
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState(true);
  const submit = () => { if (question.trim()) setAsked(true); };
  return (
    <section className="ask-screen">
      <h1 className="display screen-title">{t("ask.title")}</h1>
      {asked && <>
        <p className="question">{question || t("ask.question")}</p>
        <div className="sources" aria-label={t("ask.sources")}>
          <SourceChip name={t("source.one")} summary={t("source.summary")} why={t("source.why")} />
          <SourceChip name={t("source.two")} summary={t("source.summary")} why={t("source.why")} />
          <button className="source-chip" type="button">{t("ask.graph")}</button>
        </div>
        <p aria-live="polite" className="answer">{t("ask.answer")}</p>
        <Receipt mode={mode} t={t} />
        <div className="utility-row">
          <Button variant="ghost">{t("ask.copy")}</Button>
          <Button variant="ghost" onClick={() => setMode("local")}>{t("ask.reaskLocal")}</Button>
        </div>
      </>}
      <form className="composer" onSubmit={(event) => { event.preventDefault(); submit(); }}>
        <textarea aria-label={t("ask.placeholder")} onChange={(event) => setQuestion(event.target.value)} placeholder={t("ask.placeholder")} value={question} />
        <div className="segmented" aria-label="Synthesis mode">
          {(["auto", "local", "cloud"] as Mode[]).map((entry) => (
            <button
              className={`segment ${mode === entry ? `is-${entry}` : ""}`}
              disabled={entry === "cloud"}
              key={entry}
              onClick={() => setMode(entry)}
              title={entry === "cloud" ? t("ask.cloudUnavailable") : undefined}
              type="button"
            >
              {t(modeKeys[entry])}
            </button>
          ))}
        </div>
        <Button>{t("ask.submit")}</Button>
      </form>
    </section>
  );
}

function Status({ t }: { t: ReturnType<typeof useT> }) {
  const [saved, setSaved] = useState(false);
  const { language, setLanguage, theme, setTheme } = useUiStore();
  return (
    <section className="screen stack">
      <h1 className="display screen-title">{t("status.title")}</h1>
      <Card><h2 className="display section-heading">{t("status.engine")}</h2><div className="status-grid"><p><span className="status-dot" />{t("status.reachable")}</p><p className="engine-data">bge-m3 / qwen2.5:7b</p><p className="engine-data">1,204 files / 18,344 chunks / 512 entities / 88 topics</p></div></Card>
      <Card>
        <h2 className="display section-heading">{t("status.synthesis")}</h2>
        <div className="settings-row"><label htmlFor="model">{t("status.models")}</label><input defaultValue="qwen2.5:7b" id="model" /></div>
        <div className="utility-row"><Button onClick={() => setSaved(true)}>{t("status.save")}</Button></div>
      </Card>
      <Card>
        <h2 className="display section-heading">{t("status.about")}</h2>
        <div className="settings-row"><label>{t("status.language")}</label><div className="segmented"><button className={`segment ${language === "en" ? "is-auto" : ""}`} onClick={() => setLanguage("en")} type="button">EN</button><button className={`segment ${language === "es" ? "is-auto" : ""}`} onClick={() => setLanguage("es")} type="button">ES</button></div></div>
        <div className="settings-row"><label>{t("status.theme")}</label><div className="segmented"><button className={`segment ${theme === "dark" ? "is-auto" : ""}`} onClick={() => setTheme("dark")} type="button">{t("status.dark")}</button><button className={`segment ${theme === "light" ? "is-auto" : ""}`} onClick={() => setTheme("light")} type="button">{t("status.light")}</button></div></div>
      </Card>
      {saved && <div className="toast" role="status">{t("status.saved")}</div>}
    </section>
  );
}

function Dev({ t }: { t: ReturnType<typeof useT> }) {
  return <section className="screen"><header className="screen-header"><div><h1 className="display screen-title">{t("demo.title")}</h1><p>{t("demo.description")}</p></div></header><div className="dev-grid"><Card><Button>{t("library.addFolder")}</Button> <Button variant="ghost">{t("ask.copy")}</Button> <Button variant="danger">Remove</Button></Card><Card><Receipt mode="local" t={t} /></Card></div></section>;
}

export default function App() {
  const [route, setRoute] = useState<Route>(currentRoute);
  const { theme } = useUiStore();
  const t = useT();
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  useEffect(() => {
    const onHashChange = () => setRoute(currentRoute());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  const navigate = (next: Route) => { window.location.hash = next; };
  return <div className="app-shell"><aside className="rail"><button className="wordmark" onClick={() => navigate("library")} type="button">katsi</button><nav className="nav">{(Object.keys(routes) as Exclude<Route, "dev">[]).map((entry) => { const Icon = routes[entry].icon; return <button className={`nav-link ${route === entry ? "is-active" : ""}`} key={entry} onClick={() => navigate(entry)} type="button"><Icon size={18} weight="regular" /><span>{t(`nav.${entry}`)}</span></button>; })}</nav><div className="rail-spacer" /><p className="privacy"><span className="privacy-local">●</span> {t("privacy.local")}</p></aside><main className="main">{route === "library" && <Library t={t} />}{route === "ask" && <Ask t={t} />}{route === "status" && <Status t={t} />}{route === "dev" && <Dev t={t} />}</main></div>;
}
