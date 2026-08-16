import type { Metadata } from "next"
import Link from "next/link"
import "./globals.css"

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_STOREFRONT_URL || "http://localhost:3000"),
  title: "Fieldwork Market · Commerce SearchOps Lab",
  description: "A transparent commerce search laboratory built on public ESCI product text.",
  openGraph: {
    title: "Commerce SearchOps Lab",
    description: "Real public relevance data, inspectable BM25 ranking and governed SearchOps.",
    images: [{ url: "/searchops-social.png", width: 1200, height: 630 }]
  },
  twitter: { card: "summary_large_image", images: ["/searchops-social.png"] }
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <header className="site-header">
          <Link className="wordmark" href="/" aria-label="Fieldwork Market home">
            <span>FIELDWORK</span>
            <small>SEARCH LAB MARKET</small>
          </Link>
          <nav aria-label="Primary navigation">
            <Link href="/">Discover</Link>
            <Link href="/checkout">Cart / checkout</Link>
            <a href="http://localhost:3001">SearchOps ↗</a>
          </nav>
        </header>
        {children}
        <footer>
          <span>Commerce SearchOps Lab</span>
          <p>Public product relevance, simulated commerce. Built to make ranking decisions inspectable.</p>
        </footer>
      </body>
    </html>
  )
}
