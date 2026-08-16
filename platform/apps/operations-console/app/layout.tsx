import type { Metadata } from "next"
import Link from "next/link"
import "./globals.css"

export const metadata: Metadata = {
  title: "SearchOps Control Room · Commerce SearchOps Lab",
  description: "Govern, evaluate, approve and audit commerce search ranking changes."
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <header className="ops-header">
          <Link href="/" className="ops-logo"><span>⌁</span><div><strong>SEARCHOPS</strong><small>CONTROL ROOM / LOCAL</small></div></Link>
          <div className="system-state"><i /> CORE SYSTEM ONLINE</div>
          <nav><a href="http://localhost:3000">Storefront ↗</a><a href="http://localhost:8080/actuator/health">Health ↗</a></nav>
        </header>
        {children}
      </body>
    </html>
  )
}

