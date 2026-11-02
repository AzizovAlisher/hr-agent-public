import "next-auth";
import "next-auth/jwt";

type Org = { id: string; name: string; slug: string };

declare module "next-auth" {
  interface Session {
    user?: {
      id?: string;
      name?: string | null;
      email?: string | null;
      image?: string | null;
      role?: string;
      locale?: string;
      organization?: Org;
    };
  }

  interface User {
    role?: string;
    locale?: string;
    organization?: Org;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    role?: string;
    locale?: string;
    organization?: Org;
  }
}
