"""fma xero -- a read-only connection to every Xero organisation a pack covers.

The one tool here that keeps state and uses the network. Its sign-in lives in
`~/.config/fma/xero/` (store.py), never in a repo, a synced drive or a chat. It never
requests a scope that can change a ledger.
"""
