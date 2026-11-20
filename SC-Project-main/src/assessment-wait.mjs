export async function waitForAssessment({ request, getState, onProgress = () => {}, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)) }) {
  let settled;
  // Attach the rejection handler immediately, even while a status request is pending.
  request.then(value => { settled = {value}; }, error => { settled = {error}; });
  while (true) {
    const state = await getState();
    onProgress(state);
    if (state.status === 'cancelled') throw new Error('Assessment stopped. Completed results have been kept.');
    if (state.status === 'completed') return settled?.value;
    if (state.status === 'failed') throw settled?.error || new Error('Assessment failed. Completed controls were kept; retry to continue.');
    if (settled?.error && state.status !== 'running') throw settled.error;
    if (settled?.value) return settled.value;
    await sleep(900);
  }
}
