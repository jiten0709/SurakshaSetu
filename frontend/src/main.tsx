import { createRoot } from "react-dom/client";
import { Chat } from "./Chat";
import "./index.css";

// No StrictMode: its double-run effects would open two sessions. A reload starts a new
// conversation by design (the token is held in memory only).
createRoot(document.getElementById("root") as HTMLElement).render(<Chat />);
