// Track drawer open
document.addEventListener('DOMContentLoaded', function() {
  const drawerTriggers = document.querySelectorAll('[data-drawer-trigger]');
  drawerTriggers.forEach(trigger => {
    trigger.addEventListener('click', () => {
      const eventName = trigger.getAttribute('data-event-name') || 'unknown';
      trackBookingEvent('drawer_open', { event: eventName });
    });
  });

  // Track slot selection
  const slotButtons = document.querySelectorAll('[data-slot-time]');
  slotButtons.forEach(button => {
    button.addEventListener('click', () => {
      const eventName = button.getAttribute('data-event-name') || 'unknown';
      const time = button.getAttribute('data-slot-time') || 'unknown';
      trackBookingEvent('slot_selected', { event: eventName, time });
    });
  });
});