import { Component } from 'react'

/** Render errors visibly instead of leaving a blank screen. */
export default class ErrorBoundary extends Component {
  state = { err: null }

  static getDerivedStateFromError(err) {
    return { err }
  }

  render() {
    if (this.state.err) {
      return (
        <div className="crash">
          <h2>SatQuery AI failed to render</h2>
          <p>Fix the error below, then reload. If this persists, open DevTools (F12) → Console and copy the stack.</p>
          <pre>{String(this.state.err && (this.state.err.stack || this.state.err))}</pre>
          <button onClick={() => window.location.reload()}>Reload</button>
        </div>
      )
    }
    return this.props.children
  }
}