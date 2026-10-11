import type { SVGProps } from "react";

type IconProps = Readonly<SVGProps<SVGSVGElement>>;

export function ToolIcon(props: IconProps) {
  return <Icon {...props}><path d="m14 6 4 4 3-3a6 6 0 0 1-8 7L6 21l-3-3 7-7a6 6 0 0 1 7-8l-3 3Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" /></Icon>;
}

function Icon({ children, ...props }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height="20"
      viewBox="0 0 24 24"
      width="20"
      {...props}
    >
      {children}
    </svg>
  );
}

export function PlusIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeLinecap="round" strokeWidth="1.8" />
    </Icon>
  );
}

export function MenuIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" strokeLinecap="round" strokeWidth="1.8" />
    </Icon>
  );
}

export function SettingsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Z" stroke="currentColor" strokeWidth="1.7" />
      <path d="M19 13.2v-2.4l-2-.7a7.5 7.5 0 0 0-.7-1.7l.9-1.9-1.7-1.7-1.9.9a7.5 7.5 0 0 0-1.7-.7l-.7-2H8.8l-.7 2a7.5 7.5 0 0 0-1.7.7l-1.9-.9-1.7 1.7.9 1.9a7.5 7.5 0 0 0-.7 1.7l-2 .7v2.4l2 .7c.2.6.4 1.2.7 1.7l-.9 1.9 1.7 1.7 1.9-.9c.5.3 1.1.5 1.7.7l.7 2h2.4l.7-2c.6-.2 1.2-.4 1.7-.7l1.9.9 1.7-1.7-.9-1.9c.3-.5.5-1.1.7-1.7l2-.7Z" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.3" />
    </Icon>
  );
}

export function SendIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m4 4 17 8-17 8 3-8-3-8Z" stroke="currentColor" strokeLinejoin="round" strokeWidth="1.7" />
      <path d="M7 12h14" stroke="currentColor" strokeLinecap="round" strokeWidth="1.7" />
    </Icon>
  );
}

export function RedoIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M18 8a7 7 0 1 0 1 7M18 4v4h-4" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" />
    </Icon>
  );
}

export function StopIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect height="11" rx="1.5" stroke="currentColor" strokeWidth="1.8" width="11" x="6.5" y="6.5" />
    </Icon>
  );
}

export function MicrophoneIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect height="12" rx="3.5" stroke="currentColor" strokeWidth="1.8" width="7" x="8.5" y="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3m-4 0h8" stroke="currentColor" strokeLinecap="round" strokeWidth="1.8" />
    </Icon>
  );
}

export function PaperclipIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m9.5 12.5 5.8-5.8a3 3 0 0 1 4.2 4.2l-7.9 7.9a5 5 0 0 1-7.1-7.1l8.2-8.2" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" />
    </Icon>
  );
}

export function CloseIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m6 6 12 12M18 6 6 18" stroke="currentColor" strokeLinecap="round" strokeWidth="1.8" />
    </Icon>
  );
}

export function BookIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v16H6.5A2.5 2.5 0 0 0 4 21.5v-16ZM20 5.5A2.5 2.5 0 0 0 17.5 3H13v16h4.5a2.5 2.5 0 0 1 2.5 2.5v-16Z" stroke="currentColor" strokeLinejoin="round" strokeWidth="1.7" />
    </Icon>
  );
}

export function ChevronIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m7 9.5 5 5 5-5" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" />
    </Icon>
  );
}

export function CheckIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m5 12.5 4.2 4.2L19 7" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
    </Icon>
  );
}

export function MessageIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5 5.5h14v10H9l-4 3v-13Z" stroke="currentColor" strokeLinejoin="round" strokeWidth="1.7" />
    </Icon>
  );
}

export function RuntimeIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect height="15" rx="3" stroke="currentColor" strokeWidth="1.7" width="17" x="3.5" y="4.5" />
      <path d="M7 9h4M7 13h7M7 17h5" stroke="currentColor" strokeLinecap="round" strokeWidth="1.7" />
    </Icon>
  );
}

export function ShieldIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3.5 19 6v5.3c0 4.3-2.8 7.6-7 9.2-4.2-1.6-7-4.9-7-9.2V6l7-2.5Z" stroke="currentColor" strokeLinejoin="round" strokeWidth="1.7" />
      <path d="m8.7 12 2.1 2.1 4.6-4.6" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.7" />
    </Icon>
  );
}

export function ChartIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 19.5V5M4 19.5h16" stroke="currentColor" strokeLinecap="round" strokeWidth="1.7" />
      <path d="m7 15 3.2-3.4 2.8 2.1 4.2-5.2" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" />
      <circle cx="17.2" cy="8.5" fill="currentColor" r="1.4" />
    </Icon>
  );
}

export function BackIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m14.5 6-6 6 6 6" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" />
    </Icon>
  );
}

export function SunIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="3.5" stroke="currentColor" strokeWidth="1.7" />
      <path d="M12 2.5v2M12 19.5v2M4.5 12h-2M21.5 12h-2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M18.7 5.3l-1.4 1.4M6.7 17.3l-1.4 1.4" stroke="currentColor" strokeLinecap="round" strokeWidth="1.7" />
    </Icon>
  );
}

export function MoonIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M20 15.2A8.5 8.5 0 0 1 8.8 4a8.5 8.5 0 1 0 11.2 11.2Z" stroke="currentColor" strokeLinejoin="round" strokeWidth="1.7" />
    </Icon>
  );
}
