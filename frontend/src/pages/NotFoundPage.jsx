import { Link } from "react-router-dom";
import { EmptyState } from "../components/States";

export default function NotFoundPage() {
  return <EmptyState title="Page not found" description="The page you opened doesn't exist or isn't available to you."
    action={<Link to="/" className="btn">Go to home</Link>} />;
}
