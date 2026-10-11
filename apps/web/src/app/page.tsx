import { ChatWorkspace } from "@/components/chat-workspace";

export default function Home() {
  return (
    <>
      <a className="skip-link" href="#conversation">
        Skip to workspace
      </a>
      <ChatWorkspace />
    </>
  );
}
