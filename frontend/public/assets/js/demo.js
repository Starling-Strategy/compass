const link = document.getElementById('demoLink');
const dialog = document.getElementById('demoDialog');
const player = document.getElementById('demoPlayer');

// Progressive enhancement: the approved YouTube link works without dialog support.
if (link && dialog && player && typeof dialog.showModal === 'function') {
  link.setAttribute('aria-haspopup', 'dialog');
  link.setAttribute('aria-controls', 'demoDialog');
  link.addEventListener('click', (event) => {
    event.preventDefault();
    dialog.showModal();
    // No YouTube connection or player exists until the visitor asks for the demo.
    const iframe = document.createElement('iframe');
    iframe.src = 'https://www.youtube-nocookie.com/embed/J8KU6_e70mk?autoplay=1';
    iframe.title = 'Compass demo video';
    iframe.className = 'w-full aspect-video border-0';
    iframe.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
    iframe.allowFullscreen = true;
    iframe.referrerPolicy = 'strict-origin-when-cross-origin';
    player.replaceChildren(iframe);
  });
  // Native Close form and Escape both fire close. Destroying the frame stops audio.
  dialog.addEventListener('close', () => {
    player.replaceChildren();
    link.focus();
  });
}
