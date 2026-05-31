package com.example.blog.dto;

import com.example.blog.entity.Comment;

public class CommentDto {
    public Long id;
    public String authorName;
    public String body;

    public static CommentDto from(Comment comment) {
        CommentDto dto = new CommentDto();
        dto.id = comment.getId();
        dto.authorName = comment.getAuthorName();
        dto.body = comment.getBody();
        return dto;
    }
}
