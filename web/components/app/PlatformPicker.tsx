/**
 * PlatformPicker — "Publish to" checkboxes for scheduling a video
 * (Milestone 4.2). Lists only platforms the user has connected; the caller
 * renders it only when there's a real choice (Instagram connected), so
 * TikTok-only accounts see no change.
 *
 * Props:
 *   options   — connected platforms, [{ id, label }]
 *   selected  — chosen platform ids
 *   onChange  — called with the new selection
 */

export function PlatformPicker({
  options,
  selected,
  onChange,
}: {
  options: { id: string; label: string }[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  function toggle(id: string, checked: boolean) {
    onChange(checked ? [...selected, id] : selected.filter((value) => value !== id));
  }

  return (
    <fieldset className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-ink">
      <legend className="sr-only">Publish to</legend>
      <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted" aria-hidden="true">
        Publish to
      </span>
      {options.map((option) => (
        <label key={option.id} className="tap-target flex items-center gap-2">
          <input
            type="checkbox"
            checked={selected.includes(option.id)}
            onChange={(event) => toggle(option.id, event.target.checked)}
          />
          {option.label}
        </label>
      ))}
    </fieldset>
  );
}
