package com.example.blog.controller;

import java.util.List;
import java.util.stream.Collectors;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.example.blog.dto.CommentDto;
import com.example.blog.entity.Comment;
import com.example.blog.service.CommentService;

@RestController
@RequestMapping("/api/posts/{postId}/comments")
public class CommentController {
    private final CommentService commentService;

    @Autowired
    public CommentController(CommentService commentService) {
        this.commentService = commentService;
    }

    @GetMapping
    public List<CommentDto> list(@PathVariable Long postId) {
        return commentService.listForPost(postId).stream()
                .map(CommentDto::from)
                .collect(Collectors.toList());
    }

    @PostMapping
    public CommentDto add(@PathVariable Long postId, @RequestBody CommentDto payload) {
        Comment comment = commentService.addComment(postId, payload.authorName, payload.body);
        return CommentDto.from(comment);
    }
}
