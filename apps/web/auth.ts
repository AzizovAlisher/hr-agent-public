import NextAuth from "next-auth";
import Credentials from "next-auth/providers/credentials";

import { openIdentityTicket } from "@/lib/auth-session";

export const { handlers, auth, signIn, signOut } = NextAuth({
  trustHost: true,
  session: { strategy: "jwt", maxAge: 60 * 60 * 24 * 90 },
  pages: {
    signIn: "/login",
  },
  providers: [
    Credentials({
      name: "Server-issued session",
      credentials: {
        sessionTicket: { label: "Session ticket", type: "hidden" },
      },
      authorize: async (credentials) => {
        const identity = openIdentityTicket(
          typeof credentials?.sessionTicket === "string"
            ? credentials.sessionTicket
            : undefined,
        );
        if (!identity) return null;
        return {
          id: identity.id,
          email: identity.email,
          role: identity.role,
          locale: identity.locale,
          organization: identity.organization,
        };
      },
    }),
  ],
  callbacks: {
    jwt: ({ token, user }) => {
      if (user) {
        token.email = user.email;
        token.role = user.role;
        token.locale = user.locale;
        token.organization = user.organization;
      }
      return token;
    },
    session: ({ session, token }) => {
      if (session.user) {
        if (token.sub) session.user.id = token.sub;
        if (typeof token.email === "string") session.user.email = token.email;
        session.user.role = token.role as string | undefined;
        session.user.locale = token.locale as string | undefined;
        session.user.organization = token.organization as
          | { id: string; name: string; slug: string }
          | undefined;
      }
      // Bearer and refresh tokens live only in the separately encrypted,
      // HttpOnly API-session cookie. Browser session JSON contains identity
      // metadata only.
      return session;
    },
  },
});
