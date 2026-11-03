# Desktop spike design

The P02 window is a quiet desktop utility for proving connection and download lifetime. It retains VidGrab's dark/violet identity with a sparse layout, a visible engine state, one fixture action and an explicit Quit. A violet rail marks each download; typography and open space carry the hierarchy.

Design feasibility score: 14 (impact 3, context fit 4, implementation 4, performance 5, consistency risk 2). The scope is a lifecycle window; the full manager remains P06.

Space Grotesk 600 gives the brand/title structure; Source Sans 3 400/600 keeps labels and status readable. Latin WOFF assets ship with the frontend through locked Fontsource packages, so launch does not request remote fonts. Colors are CSS variables in `src/style.css`; spacing follows an 8px rhythm, controls are at least 44px tall and focus is visible. Status changes use semantic live regions, errors use an alert, and progress uses a native progress element. There is no decorative animation.

Missing tray support leaves the window visible. Closing to the tray is enabled only after checking for a Linux StatusNotifierWatcher and successfully creating the tray icon. This check is a capability hint; actual tray behavior still requires the platform validation matrix.
