import React from "react";
import {
  GlobeIcon, DatabaseIcon, BookIcon, CloudIcon, CalcIcon, CurrencyIcon,
  ChartIcon, GitIcon, PinIcon, ClockIcon, ArrowRightIcon,
} from "./Icons";

// The tools the agent can actually call — mirrors the `tools = [...]` list
// bound to the model in backend/chatbot_backend.py. Keep the two in sync:
// spec §08 says never show a tool the backend can't use. (fetch_url and
// lookup_pincode exist in tools.py but are NOT bound, so they're not listed.)
// The assistant chooses tools itself; a card just drops an example prompt
// into the composer — it never calls a tool directly.
export const TOOLS = [
  { id: "web_search",       name: "Web Search",         icon: GlobeIcon,    desc: "Finds up-to-date information on the web — news, sports, prices.", example: "What are the top technology headlines today?" },
  { id: "rag_tool",         name: "Knowledge RAG",      icon: DatabaseIcon, desc: "Answers from the indexed PDF: Ethics of Data Science, Chapter 10.", example: "According to the Ethics of Data Science chapter, what is informed consent?" },
  { id: "wikipedia_search", name: "Wikipedia",          icon: BookIcon,     desc: "Looks up encyclopedia summaries on people, places and topics.", example: "Give me a short Wikipedia summary of Alan Turing." },
  { id: "get_weather",      name: "Weather",            icon: CloudIcon,    desc: "Current weather conditions for a city or place.", example: "What's the weather in Mumbai right now?" },
  { id: "calculator",       name: "Calculator",         icon: CalcIcon,     desc: "Exact arithmetic — add, subtract, multiply, divide.", example: "Calculate 2450 multiplied by 0.185." },
  { id: "convert_currency", name: "Currency Converter", icon: CurrencyIcon, desc: "Converts amounts between currencies at current rates.", example: "Convert 100 USD to INR." },
  { id: "get_stock_price",  name: "Stock Information",  icon: ChartIcon,    desc: "Latest daily closing price for a stock symbol.", example: "What was the latest closing price of AAPL?" },
  { id: "github_search",    name: "GitHub",             icon: GitIcon,      desc: "Searches public GitHub repositories and users.", example: "Find popular GitHub repositories for LangGraph." },
  { id: "geo_lookup",       name: "Location",           icon: PinIcon,      desc: "Approximate location of an IP address.", example: "Where is the IP address 8.8.8.8 located?" },
  { id: "current_datetime", name: "Date & Time",        icon: ClockIcon,    desc: "Current date and time in any time zone.", example: "What time is it in Tokyo right now?" },
];

export default function ToolDirectory({ onTry }) {
  return (
    <div className="tools-view">
      <div className="tools-inner">
        <header className="tools-head">
          <div className="eyebrow">AI Tools</div>
          <h1 className="page-title">What SynapseAI can use</h1>
          <p className="page-sub">
            The assistant picks the right tool on its own while it answers. Choose a tool to
            put an example prompt in the composer — you can edit it before sending.
          </p>
        </header>

        <ul className="tool-grid" role="list">
          {TOOLS.map(({ id, name, icon: Icon, desc, example }) => (
            <li key={id}>
              <button type="button" className="tool-card" onClick={() => onTry(example)}>
                <span className="tool-icon"><Icon size={18} /></span>
                <span className="tool-name">{name}</span>
                <span className="tool-desc">{desc}</span>
                <span className="tool-try">Try an example <ArrowRightIcon size={13} /></span>
              </button>
            </li>
          ))}
        </ul>

        <p className="tools-note">
          Also in the composer: attach a PDF, DOCX or image, and voice chat (Chrome / Edge).
        </p>
      </div>
    </div>
  );
}
