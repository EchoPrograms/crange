async page => {
  const checks = [];
  const errors = [];
  const check = (condition, label) => { if (!condition) throw new Error(label); checks.push(label); };
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({width: 1440, height: 900});
  await page.goto('http://127.0.0.1:8786/tests/fixtures/guac_session_tabs.html');
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.waitForSelector('.cr-session-tabs');
  await page.waitForTimeout(250);
  const tabs = page.getByRole('tab');
  check(await tabs.count() === 4, 'Four synthetic sessions render as native-client tabs');
  check(await page.locator('.client-tile.focused:visible').count() === 1, 'Only one full-size active client tile is visible');
  const activeDisplay = await page.locator('.client-tile.focused .display').boundingBox();
  check(activeDisplay && activeDisplay.width >= 1400 && activeDisplay.height > 700, 'The active session uses the available desktop viewport');
  check(await page.locator('.client-tile-header:visible').count() === 0, 'Tab titles replace duplicate native tile headers only in tab mode');
  const handle = await page.getByRole('button', {name: 'Show session controls'}).boundingBox();
  check(handle && handle.x <= 1 && handle.y > 850, 'The minimized toolbar handle defaults to the bottom-left corner');
  await page.screenshot({path: 'output/playwright/guac-session-tabs-desktop.png', fullPage: true});

  await tabs.filter({hasText: 'Synthetic Bravo'}).click();
  await page.getByText('Synthetic Bravo', {exact: true}).last().waitFor();
  check(await page.locator('.client-tile.focused .fixture-host').textContent() === 'Synthetic Bravo', 'Selecting a tab shows that connection at full size');
  check(await page.locator('.client-tile.focused:visible').count() === 1, 'Switching tabs keeps exactly one visible client');
  await page.getByRole('button', {name: 'Close Synthetic Bravo', exact: true}).click();
  check(await page.getByRole('tab').count() === 3, 'Closing a tab removes only its own connection');
  check(await page.locator('.client-tile.focused .fixture-host').textContent() === 'Synthetic Alpha', 'Closing the active tab returns focus to a remaining session');
  check(!(await page.evaluate(() => window.__fixtureGroupDisconnect)), 'Closing a tab never disconnects the whole group');

  await page.reload();
  await page.waitForSelector('.cr-session-tabs');
  await page.waitForTimeout(150);
  await page.getByRole('button', {name: 'Show session controls'}).click();
  await page.getByRole('button', {name: 'Options', exact: true}).click();
  const popup = await page.locator('.cr-session-options').boundingBox();
  const viewport = await page.evaluate(() => ({width: innerWidth, height: innerHeight}));
  check(popup && popup.x >= 0 && popup.y >= 0 && popup.x + popup.width <= viewport.width + 1 && popup.y + popup.height <= viewport.height + 1, 'Options popup flips above the bottom toolbar and stays in the desktop viewport');
  await page.getByRole('button', {name: 'Close session options'}).click();

  const dragToolbar = async (targetX, targetY) => {
    await page.locator('.cr-session-toolbar').waitFor({state: 'visible'});
    await page.waitForTimeout(120);
    const box = await page.locator('.cr-session-drag').boundingBox();
    await page.mouse.move(box.x + 10, box.y + 10);
    await page.mouse.down();
    await page.mouse.move(targetX, targetY, {steps: 5});
    await page.mouse.up();
    await page.waitForTimeout(50);
  };
  const stateKey = id => 'crange-session/' + encodeURIComponent('synthetic-source/browser-fixture') + '/connection/' + encodeURIComponent('synthetic-source/' + id) + '/toolbar';
  await dragToolbar(290, 180);
  await page.getByRole('button', {name: 'Hide session toolbar'}).click();
  await page.waitForFunction(() => !angular.element(document.querySelector('crange-session-toolbar')).scope().crSession.visible);
  const alphaState = await page.evaluate(key => JSON.parse(localStorage.getItem(key)), stateKey('synthetic-connection-alpha'));
  check(alphaState && alphaState.position.x > 200 && alphaState.position.y > 100 && alphaState.hidden, 'Alpha stores its expanded position and closed state per connection');

  await tabs.filter({hasText: 'Synthetic Bravo'}).click();
  check((await page.getByRole('button', {name: 'Show session controls'}).boundingBox()).x <= 1, 'A fresh connection gets its own bottom-left minimized dock');
  await page.getByRole('button', {name: 'Show session controls'}).click();
  await page.locator('.cr-session-toolbar').waitFor({state: 'visible'});
  await dragToolbar(560, 320);
  await page.getByRole('button', {name: 'Hide session toolbar'}).click();
  await page.waitForFunction(() => !angular.element(document.querySelector('crange-session-toolbar')).scope().crSession.visible);
  const bravoState = await page.evaluate(key => JSON.parse(localStorage.getItem(key)), stateKey('synthetic-connection-bravo'));
  check(bravoState.position.x > 450 && bravoState.position.y > 250 && bravoState.hidden, 'Bravo saves an independent expanded position and open state');

  await tabs.filter({hasText: 'Synthetic Alpha'}).click();
  check(!(await page.locator('.cr-session-toolbar').isVisible()), 'Switching back restores Alpha as minimized');
  await page.getByRole('button', {name: 'Show session controls'}).click();
  await page.waitForTimeout(100);
  const alphaRect = await page.locator('.cr-session-shell').boundingBox();
  check(alphaRect.x > 200 && alphaRect.x < 400 && alphaRect.y > 100, 'Switching back restores Alpha expanded coordinates');
  await page.getByRole('button', {name: 'Hide session toolbar'}).click();
  await page.waitForFunction(() => !angular.element(document.querySelector('crange-session-toolbar')).scope().crSession.visible);
  await page.reload();
  await page.waitForSelector('.cr-session-tabs');
  await page.waitForFunction(() => document.querySelector('.cr-session-reopen') && getComputedStyle(document.querySelector('.cr-session-reopen')).display !== 'none');
  await page.getByRole('button', {name: 'Show session controls'}).click();
  await page.waitForTimeout(100);
  const afterRefresh = await page.locator('.cr-session-shell').boundingBox();
  check(afterRefresh.x > 200 && afterRefresh.x < 400 && afterRefresh.y > 100, 'Alpha layout persists after refresh');
  await page.getByRole('button', {name: 'Hide session toolbar'}).click();
  await page.waitForFunction(() => !angular.element(document.querySelector('crange-session-toolbar')).scope().crSession.visible);

  await page.setViewportSize({width: 390, height: 844});
  await page.waitForTimeout(100);
  const mobile = await page.evaluate(() => ({width: innerWidth, documentWidth: document.documentElement.scrollWidth, tabsWidth: document.querySelector('.cr-session-tabs').clientWidth, tabsScroll: document.querySelector('.cr-session-tabs').scrollWidth}));
  check(mobile.documentWidth <= mobile.width && mobile.tabsScroll > mobile.tabsWidth, 'Mobile tab strip scrolls horizontally without page overflow');
  await page.getByRole('button', {name: 'Show session controls'}).click();
  await page.getByRole('button', {name: 'Options', exact: true}).click();
  const mobilePopup = await page.locator('.cr-session-options').boundingBox();
  check(mobilePopup && mobilePopup.x >= 0 && mobilePopup.y >= 0 && mobilePopup.x + mobilePopup.width <= 391 && mobilePopup.y + mobilePopup.height <= 845, 'Options popup remains inside the mobile viewport');
  await page.screenshot({path: 'output/playwright/guac-session-tabs-mobile.png', fullPage: true});
  check(errors.length === 0, 'Synthetic Guacamole tab fixture has no JavaScript errors');
  return {checks, errors, syntheticSessions: 4, controllerOrGuestRequests: 0};
}
