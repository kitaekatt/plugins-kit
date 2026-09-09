# Styling Reference

ANSI/Unicode patterns for common visual customizations. Load only when the user names a styling concept (color, threshold, progress bar, separator, etc).

## Colors (256-color palette)

| Use | ANSI code | Notes |
|-----|-----------|-------|
| Default gray | `\033[38;5;250m` | The plugin default |
| Dim gray | `\033[38;5;238m` | Used for separators |
| White (bright) | `\033[38;5;255m` | |
| Red | `\033[38;5;196m` | Threshold "red" |
| Orange | `\033[38;5;208m` | Threshold "orange" |
| Yellow | `\033[38;5;226m` | Bright yellow |
| Yellow (dim) | `\033[38;5;220m` | Softer yellow |
| Green | `\033[38;5;46m` | |
| Cyan | `\033[38;5;51m` | |
| Blue | `\033[38;5;33m` | |
| Magenta | `\033[38;5;201m` | |

Always end with `\033[0m` to reset. Use `\033[2m` for dim, `\033[1m` for bold. Truecolor is `\033[38;2;R;G;Bm` if 256-color isn't enough.

## Threshold-based coloring

The default statusline expresses **capacity remaining** (higher = better) for all percentages, so colors trigger when the value drops AT OR BELOW the threshold:

```bash
if   [ "$VALUE" -le "$RED_AT" ];    then CLR="\033[38;5;196m"
elif [ "$VALUE" -le "$ORANGE_AT" ]; then CLR="\033[38;5;208m"
else                                     CLR="\033[38;5;250m"
fi
```

Default thresholds in `scripts/statusline.sh`:

| Var | Default | Meaning |
|---|---|---|
| `STATUSLINE_CTX_ORANGE_AT` | `70` | Context turns orange when remaining <= 70% |
| `STATUSLINE_CTX_RED_AT` | `30` | Context turns red when remaining <= 30% |
| `STATUSLINE_SESS_ORANGE_AT` | `30` | 5-hour budget turns orange when remaining <= 30% |
| `STATUSLINE_SESS_RED_AT` | `10` | 5-hour budget turns red when remaining <= 10% |
| `STATUSLINE_WEEK_ORANGE_AT` | `30` | 7-day budget turns orange when remaining <= 30% |
| `STATUSLINE_WEEK_RED_AT` | `10` | 7-day budget turns red when remaining <= 10% |

Override in `settings.json` -> `env`. If you build a custom statusline that displays "% used" instead, flip the comparison back to `-ge` and pick used-side thresholds (e.g. orange at 70% used).

## Progress bars

Filled-block bar (10 cells, percentage 0-100):

```bash
filled=$(( PCT / 10 ))
empty=$(( 10 - filled ))
bar="$(printf '%0.s█' $(seq 1 $filled))$(printf '%0.s░' $(seq 1 $empty))"
```

Other styles:

| Style | Filled | Empty | Notes |
|-------|--------|-------|-------|
| Solid blocks | `█` | `░` | Default |
| Heavy/light | `▰` | `▱` | Sparser |
| Brackets | `=` | `-` | ASCII fallback |
| Dots | `●` | `○` | Round |

Color the whole bar one color, OR color filled vs empty separately, OR use a gradient (see below).

## Gradients

For a smooth red->yellow->green over a 0-100 range, sample the 256-color palette at: red `196`, orange `208`, yellow `226`, green-yellow `190`, green `46`. Map percentage buckets to these codes; do not interpolate truecolor unless the user explicitly asks -- buckets render fine and stay terminal-safe.

If the user wants a gradient on a progress bar specifically: color each filled cell by its position. Example for a 10-cell red->green bar at 80%:

```bash
colors=(196 202 208 214 220 226 190 154 118 46)
bar=""
for i in $(seq 0 9); do
    if [ $i -lt $filled ]; then
        bar="$bar\033[38;5;${colors[i]}m█"
    else
        bar="$bar\033[38;5;238m░"
    fi
done
bar="$bar\033[0m"
```

## Separators

| Char | Name | Notes |
|------|------|-------|
| ` │ ` | Light vertical (U+2502) | Plugin default |
| ` ▸ ` | Black right-pointing small triangle | |
| ` › ` | Single right chevron | |
| `  ` | Powerline right arrow (U+E0B0) | Requires nerd-font |
| ` • ` | Bullet | |
| ` / ` | Slash | ASCII fallback |
| ` | ` | Pipe | ASCII fallback |

Wrap separators in a dim color (`\033[2m\033[38;5;238m ... \033[0m`) so segments stand out.

## Powerline mode (advanced)

Powerline replaces space-padded segments with arrow-capped colored rectangles. It requires a nerd-font in the user's terminal. If the user asks for powerline:

1. Confirm they have a nerd-font installed.
2. Each segment renders as: `\033[48;5;<bg>m\033[38;5;<fg>m TEXT \033[0m\033[38;5;<bg>m\033[48;5;<next-bg>m\033[0m`
3. Last segment ends with `\033[38;5;<bg>m\033[0m` (no next-bg).

Don't volunteer powerline. Mention it only if the user names it.

## Themes (named presets)

Common community themes referenced by name:

| Name | Vibe |
|------|------|
| Default (plugin) | Gray with red/orange thresholds |
| Catppuccin Mocha | Soft pastels on dark |
| Dracula | Saturated purple/cyan |
| Nord | Cool blues/grays |
| Gruvbox | Warm earth tones |
| Tokyo Night | Deep blue-purple |
| Solarized | Beige/teal accents |

If the user names one, look up the official palette and map to 256-color codes. Don't ship them in the default script -- they balloon the script for users who don't want them.
