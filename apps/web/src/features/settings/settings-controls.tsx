import { CloseIcon, PlusIcon } from "@/components/icons";

type FeatureOption = readonly [label: string, key: string, enabled: boolean];

export function FeatureSettingsPanel({
  disabled,
  enabled,
  hidden,
  onEnabledChange,
  onOptionChange,
  options,
  title,
}: Readonly<{
  disabled: boolean;
  enabled: boolean;
  hidden: boolean;
  onEnabledChange: (enabled: boolean) => void;
  onOptionChange: (key: string, enabled: boolean) => void;
  options: readonly FeatureOption[];
  title: string;
}>) {
  return (
    <section className="settings-view feature-settings-view" hidden={hidden}>
      <div className="feature-master-row">
        <div><h3>{title}</h3></div>
        <label aria-label={`Enable ${title}`} className="toggle-control">
          <span className="sr-only">Enable {title}</span>
          <input checked={enabled} disabled={disabled} onChange={(event) => onEnabledChange(event.target.checked)} type="checkbox" />
          <span aria-hidden="true" />
        </label>
      </div>
      <div aria-disabled={!enabled} className={`feature-option-list ${enabled ? "" : "is-disabled"}`}>
        {options.map(([label, key, checked]) => (
          <label aria-label={label} className="feature-option" key={key}>
            <span><strong>{label}</strong></span>
            <input checked={checked} disabled={disabled || !enabled} onChange={(event) => onOptionChange(key, event.target.checked)} type="checkbox" />
          </label>
        ))}
      </div>
    </section>
  );
}
export function GuardrailAdder({
  draft,
  id,
  items,
  label,
  onAdd,
  onChange,
  onRemove,
  placeholder,
}: Readonly<{
  draft: string;
  id: string;
  items: readonly string[];
  label: string;
  onAdd: () => void;
  onChange: (value: string) => void;
  onRemove: (value: string) => void;
  placeholder: string;
}>) {
  return (
    <section className="guardrail-group" aria-labelledby={`${id}-heading`}>
      <h4 id={`${id}-heading`}>{label}</h4>
      <div className="adder-row">
        <label htmlFor={id}>{label}</label>
        <input
          id={id}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          value={draft}
        />
        <button
          aria-label={`Add to ${label.toLowerCase()}`}
          className="add-rule-button"
          disabled={!draft.trim()}
          onClick={onAdd}
          type="button"
        >
          <PlusIcon />
        </button>
      </div>
      <div className="rule-chips">
        {items.map((item) => (
          <span className="rule-chip" key={item}>
            {item}
            <button aria-label={`Remove ${item}`} onClick={() => onRemove(item)} type="button">
              <CloseIcon />
            </button>
          </span>
        ))}
      </div>
    </section>
  );
}
