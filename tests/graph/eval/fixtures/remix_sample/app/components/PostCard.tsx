interface Props {
  title: string;
  author: string;
}

export default function PostCard({ title, author }: Props) {
  return (
    <div className="post-card">
      <h3>{title}</h3>
      <span>{author}</span>
    </div>
  );
}
