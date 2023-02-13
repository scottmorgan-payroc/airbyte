import classNames from "classnames";
import { PropsWithChildren } from "react";
import { Link as ReactRouterLink, To } from "react-router-dom";

import styles from "./Link.module.scss";

export interface LinkProps {
  className?: string;
  opensInNewTab?: boolean;
}

interface InternalLinkProps extends LinkProps {
  to: To;
}

export const Link: React.FC<PropsWithChildren<InternalLinkProps>> = ({
  children,
  className,
  to,
  opensInNewTab = false,
  ...props
}) => {
  return (
    <ReactRouterLink
      {...props}
      className={classNames(styles.link, className)}
      rel={opensInNewTab ? "noopener noreferrer" : undefined}
      target={opensInNewTab ? "_blank" : "_self"}
      to={to}
    >
      {children}
    </ReactRouterLink>
  );
};
